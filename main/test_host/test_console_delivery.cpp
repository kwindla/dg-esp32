#include "dg_console_delivery.h"
#include <gtest/gtest.h>
#include <string>
TEST(ConsoleDelivery, AbsentHostExpiresOnlyInDrainWorker) {
    long long now=0;int attempts=0;
    EXPECT_FALSE(dg_console::deliver("hello",5,
        [&](const char*,size_t){++attempts;return 0;},[&]{return now;},[&]{now+=1000;}));
    EXPECT_EQ(now,1000000);EXPECT_EQ(attempts,1000);
}
TEST(ConsoleDelivery, HostReturnsBeforeDeadlineWithoutLosingAck) {
    long long now=0;std::string output;
    EXPECT_TRUE(dg_console::deliver("OK #61 connect\n",15,
        [&](const char* p,size_t n){if(now<50000)return 0;output.append(p,n);return int(n);},
        [&]{return now;},[&]{now+=1000;}));
    EXPECT_EQ(output,"OK #61 connect\n");EXPECT_EQ(now,50000);
}
TEST(ConsoleDelivery, DisconnectDuringRecordIsBounded) {
    long long now=0;int accepted=0;std::string text(5000,'a');
    EXPECT_FALSE(dg_console::deliver(text.data(),text.size(),
        [&](const char*,size_t n){if(accepted)return 0;accepted=int(n);return accepted;},
        [&]{return now;},[&]{now+=1000;}));
    EXPECT_EQ(accepted,2048);EXPECT_EQ(now,1000000);
}
TEST(ConsoleDelivery, ConnectedRecordsKeepBytesAndOrdering) {
    std::string input(12500,'x'),output;int calls=0;
    EXPECT_TRUE(dg_console::deliver(input.data(),input.size(),
        [&](const char* p,size_t n){++calls;EXPECT_LE(n,2048u);output.append(p,n);return int(n);},
        []{return 0LL;},[]{FAIL();}));
    EXPECT_EQ(output,input);EXPECT_EQ(calls,7);
}
TEST(ConsoleProducer, FullQueueDropsWithoutRetry) {
    int calls=0;
    EXPECT_EQ(dg_console::enqueue_record("line",4,
        [&](const char*,size_t){++calls;return false;}),dg_console::Admission::Full);
    EXPECT_EQ(calls,1);
}
TEST(ConsoleProducer, EnqueuesEntireEvidenceRecordOnce) {
    std::string input(16384,'x'),output;int calls=0;
    EXPECT_EQ(dg_console::enqueue_record(input.data(),input.size(),
        [&](const char* p,size_t n){++calls;output.assign(p,n);return true;}),dg_console::Admission::Queued);
    EXPECT_EQ(calls,1);EXPECT_EQ(output,input);
}
TEST(ConsoleProducer, NoHostPresenceDependency) {
    // A connected flag can be false during DTLS or true with no host reader.
    // Producer admission has no dependency on either host state.
    bool space=true;
    EXPECT_EQ(dg_console::enqueue_record("ACK",3,
        [&](const char*,size_t){return space;}),dg_console::Admission::Queued);
}
