#include "msg_queue.h"
#include <pipecat/errors.h>
#include <gtest/gtest.h>
#include <string>
#include <vector>
using namespace pipecat::esp_peer;
TEST(Messages, ClassifiesOnlyTopLevelTypeAndRejectsMalformed) {
    EXPECT_EQ(classify_message(R"({"type":"bot-ready"})"), MessageKind::BotReady);
    EXPECT_EQ(classify_message(R"({"type":"signalling","message":{"type":"peerLeft"}})"), MessageKind::Control);
    EXPECT_EQ(classify_message(R"({"data":{"type":"bot-ready"},"type":"text"})"), MessageKind::Normal);
    EXPECT_EQ(classify_message(R"({"type":"bot\u002dready"})"), MessageKind::BotReady);
    for (const char* bad : {"", "[]", "null", "1", "{}{}", "{", R"({"type":"bot-ready","type":"x"})", R"({"type":42})", R"({"type":null})", R"({"type":{}})", R"({"type":[]})"})
        EXPECT_EQ(classify_message(bad), MessageKind::Invalid) << bad;
    EXPECT_FALSE(valid_message_json(std::string("{}\0", 3)));
    EXPECT_FALSE(valid_message_json(R"({"a":{"b":{}}})", 2));
    EXPECT_TRUE(valid_message_json(R"({"a":{"b":{}}})", 3));
    EXPECT_TRUE(valid_message_json(R"({"data":"[{\"type\":\"bot-ready\"}]"})", 1));
}
TEST(Messages, ReservedSlotAndNoNullOnWire) {
    std::vector<char> memory(32 * 16384);
    MessageQueue q(memory.data(), memory.size());
    for (int i = 0; i < 31; ++i) EXPECT_EQ(q.push("{}", i), MessageQueue::Result::Queued);
    EXPECT_EQ(q.push("{}", 31), MessageQueue::Result::Full);
    EXPECT_EQ(q.push(R"({"type":"bot-ready"})", 32), MessageQueue::Result::Queued);
    EXPECT_EQ(q.depth(), 32u);
    EXPECT_EQ(q.oldest_age_ms(100), 100);
    char out[64]; std::fill_n(out, 64, '!');
    size_t size; int64_t arrival;
    ASSERT_TRUE(q.pop(out, sizeof out, size, arrival));
    EXPECT_EQ(std::string_view(out, size), R"({"type":"bot-ready"})");
    EXPECT_EQ(out[size], '!'); EXPECT_EQ(arrival, 32);
    for (int i = 0; i < 31; ++i) { ASSERT_TRUE(q.pop(out, sizeof out, size, arrival)); EXPECT_EQ(arrival, i); }
    EXPECT_FALSE(q.pop(out, sizeof out, size, arrival));
    EXPECT_EQ(q.counters().overflow, 1u);
}
TEST(Messages, BotReadySurvivesControlFloodAndReset) {
    std::vector<char> memory(32 * 100);
    MessageQueue q(memory.data(), memory.size(), 100);
    EXPECT_EQ(q.push(R"({"type":"signalling"})", 1), MessageQueue::Result::Queued);
    EXPECT_EQ(q.push(R"({"type":"bot-ready"})", 2), MessageQueue::Result::ReplacedControl);
    EXPECT_EQ(q.push(R"({"type":"signalling"})", 3), MessageQueue::Result::Full);
    size_t size; int64_t arrival; char out[100];
    EXPECT_THROW(q.pop(out, 1, size, arrival), std::invalid_argument);
    EXPECT_EQ(q.depth(), 1u);
    ASSERT_TRUE(q.pop(out, sizeof out, size, arrival));
    EXPECT_EQ(std::string_view(out, size), R"({"type":"bot-ready"})");
    q.reset(); EXPECT_EQ(q.depth(), 0u); EXPECT_EQ(q.counters().overflow, 0u);
}
TEST(Messages, SizeMatrixAndReassembledMessages) {
    std::vector<char> memory(32 * 16384);
    MessageQueue q(memory.data(), memory.size());
    for (size_t n : {1024, 15 * 1024, 16384, 17 * 1024}) {
        std::string wire = "{\"data\":\"" + std::string(n - 11, 'x') + "\"}";
        ASSERT_EQ(wire.size(), n);
        if (n <= 16384) {
            EXPECT_NO_THROW(check_message_size(wire.size()));
            // SCTP supplies a complete message regardless of network fragmentation.
            EXPECT_EQ(q.push(wire, 0), MessageQueue::Result::Queued);
            std::vector<char> out(n); size_t size; int64_t arrival;
            ASSERT_TRUE(q.pop(out.data(), out.size(), size, arrival));
            EXPECT_EQ(std::string(out.data(), size), wire);
        } else {
            EXPECT_THROW(check_message_size(wire.size()), pipecat::MessageTooLargeError);
            EXPECT_EQ(q.push(wire, 0), MessageQueue::Result::Oversize);
        }
    }
    EXPECT_EQ(q.push("{", 0), MessageQueue::Result::Invalid);
    EXPECT_EQ(q.counters().oversize, 1u);
    EXPECT_EQ(q.counters().invalid, 1u);
}
TEST(Messages, RingReuseAndStorageValidation) {
    std::vector<char> memory(32 * 32);
    EXPECT_THROW(MessageQueue(nullptr, memory.size(), 32), std::invalid_argument);
    EXPECT_THROW(MessageQueue(memory.data(), 31, 32), std::invalid_argument);
    MessageQueue q(memory.data(), memory.size(), 32);
    char out[32]; size_t size; int64_t arrival;
    for (int round = 0; round < 5; ++round) {
        for (int i = 0; i < 31; ++i) ASSERT_EQ(q.push("{}", i), MessageQueue::Result::Queued);
        for (int i = 0; i < 31; ++i) { ASSERT_TRUE(q.pop(out, sizeof out, size, arrival)); EXPECT_EQ(arrival, i); }
    }
}
