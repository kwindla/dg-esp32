#include "msg_queue.h"
#include <nlohmann/json.hpp>
#include <pipecat/errors.h>
#include <algorithm>
#include <cstring>
#include <stdexcept>

namespace pipecat::esp_peer {
namespace {
// SAX avoids constructing JSON trees in the producer callback. A string token
// may allocate at most the already bounded input length.
struct Classifier : nlohmann::json_sax<nlohmann::json> {
    explicit Classifier(size_t limit) : limit(limit) {}
    size_t limit, depth = 0;
    bool root_object = false, type_key = false, seen_type = false;
    MessageKind kind = MessageKind::Normal;
    bool scalar(bool string_value=false) { if(type_key && !string_value)return false; type_key = false; return depth != 0; }
    bool null() override { return scalar(); }
    bool boolean(bool) override { return scalar(); }
    bool number_integer(number_integer_t) override { return scalar(); }
    bool number_unsigned(number_unsigned_t) override { return scalar(); }
    bool number_float(number_float_t, const string_t&) override { return scalar(); }
    bool binary(binary_t&) override { return false; }
    bool string(string_t& value) override {
        if (type_key) {
            if (value == "bot-ready") kind = MessageKind::BotReady;
            else if (value == "signalling") kind = MessageKind::Control;
        }
        return scalar(true);
    }
    bool key(string_t& value) override {
        type_key = depth == 1 && value == "type";
        if (type_key) {
            if (seen_type) return false; // no ambiguous control classification
            seen_type = true;
        }
        return true;
    }
    bool start_object(std::size_t) override {
        if(type_key)return false;
        if (depth == 0) root_object = true;
        type_key = false;
        return ++depth <= limit;
    }
    bool end_object() override { --depth; return true; }
    bool start_array(std::size_t) override {
        if (!depth || type_key) return false;
        type_key = false;
        return ++depth <= limit;
    }
    bool end_array() override { --depth; return true; }
    bool parse_error(std::size_t, const std::string&, const nlohmann::detail::exception&) override {
        return false;
    }
};
}
MessageKind classify_message(std::string_view text, size_t max_depth) {
    if (text.empty() || text.find('\0') != std::string_view::npos) return MessageKind::Invalid;
    Classifier parser(max_depth);
    bool valid = nlohmann::json::sax_parse(text.begin(), text.end(), &parser);
    return valid && parser.root_object ? parser.kind : MessageKind::Invalid;
}
bool valid_message_json(std::string_view text, size_t max_depth) {
    return classify_message(text, max_depth) != MessageKind::Invalid;
}
void check_message_size(size_t size, size_t limit) {
    if (size > limit) throw MessageTooLargeError();
}
MessageQueue::MessageQueue(char* storage, size_t bytes, size_t limit)
    : storage_(storage), limit_(limit) {
    if (!storage || !limit || limit > 16384 || bytes / capacity < limit)
        throw std::invalid_argument("Invalid message queue storage/limit");
}
MessageQueue::Result MessageQueue::push(std::string_view message, int64_t now_ms) {
    if (message.size() > limit_) { ++counters_.oversize; return Result::Oversize; }
    return push_classified(message, now_ms, classify_message(message));
}
MessageQueue::Result MessageQueue::push_classified(std::string_view message, int64_t now_ms, MessageKind kind) {
    if (message.size() > limit_) { ++counters_.oversize; return Result::Oversize; }
    if (kind == MessageKind::Invalid) { ++counters_.invalid; return Result::Invalid; }
    bool control = kind == MessageKind::Control || kind == MessageKind::BotReady;
    size_t index;
    Result result = Result::Queued;
    if (control) {
        if (control_) {
            ++counters_.overflow;
            if (bot_ready_ || kind != MessageKind::BotReady) return Result::Full;
            result = Result::ReplacedControl;
        }
        index = capacity - 1;
        control_ = true;
        bot_ready_ = kind == MessageKind::BotReady;
    } else {
        if (count_ == capacity - 1) { ++counters_.overflow; return Result::Full; }
        index = (head_ + count_++) % (capacity - 1);
    }
    std::memcpy(storage_ + index * limit_, message.data(), message.size());
    slots_[index] = {message.size(), now_ms};
    return result;
}
bool MessageQueue::pop(char* output, size_t bytes, size_t& size, int64_t& arrival) {
    size = 0;
    if (!depth()) return false;
    size_t index = control_ ? capacity - 1 : head_;
    const auto slot = slots_[index];
    if (!output || bytes < slot.size) throw std::invalid_argument("Message output too small");
    std::memcpy(output, storage_ + index * limit_, slot.size);
    size = slot.size;
    arrival = slot.arrival_ms;
    if (control_) control_ = bot_ready_ = false;
    else { head_ = (head_ + 1) % (capacity - 1); --count_; }
    return true;
}
int64_t MessageQueue::oldest_age_ms(int64_t now) const {
    if (!depth()) return 0;
    int64_t oldest = control_ ? slots_.back().arrival_ms : slots_[head_].arrival_ms;
    if (count_) oldest = std::min(oldest, slots_[head_].arrival_ms);
    return std::max(int64_t(0), now - oldest);
}
void MessageQueue::reset() {
    head_ = count_ = 0;
    control_ = bot_ready_ = false;
    counters_ = {};
}
}
