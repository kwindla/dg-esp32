#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <string_view>

namespace pipecat::esp_peer {
// Validates one complete JSON object, with a bounded nesting depth. No DOM.
bool valid_message_json(std::string_view text, size_t max_depth = 16);
enum class MessageKind { Normal, Control, BotReady, Invalid };
MessageKind classify_message(std::string_view text, size_t max_depth = 16);
void check_message_size(size_t size, size_t limit = 16384);

// Caller owns 32 * limit bytes (explicit PSRAM on device), and serializes access.
// 31 ordinary slots + one reserved control slot. Bot-ready can replace a pending
// control message; ordinary messages can never evict it. FIFO within each class,
// control first. Input is one complete SCTP message; no invented fragmentation
// framing is placed on the wire. esp_peer owns SCTP fragmentation/reassembly.
class MessageQueue {
public:
    static constexpr size_t capacity = 32;
    enum class Result { Queued, ReplacedControl, Full, Oversize, Invalid };
    struct Counters { uint64_t overflow = 0, oversize = 0, invalid = 0; };
    MessageQueue(char* storage, size_t storage_bytes, size_t limit = 16384);
    Result push(std::string_view message, int64_t now_ms);
    // Native callbacks classify outside their short queue critical section.
    Result push_classified(std::string_view message, int64_t now_ms, MessageKind kind);
    // Copies exactly size bytes, never a trailing NUL. Too-small output throws
    // without consuming the message.
    bool pop(char* output, size_t capacity_bytes, size_t& size, int64_t& arrival_ms);
    size_t depth() const { return count_ + (control_ ? 1 : 0); }
    // True when any next push is accepted: no pending control and ordinary room.
    // A consumer that checks this first never needs to drop for capacity.
    bool can_push() const { return !control_ && count_ < capacity - 1; }
    int64_t oldest_age_ms(int64_t now_ms) const;
    const Counters& counters() const { return counters_; }
    void reset();
private:
    struct Slot { size_t size = 0; int64_t arrival_ms = 0; };
    char* storage_;
    size_t limit_, head_ = 0, count_ = 0;
    bool control_ = false, bot_ready_ = false;
    std::array<Slot, capacity> slots_{};
    Counters counters_;
};
}
