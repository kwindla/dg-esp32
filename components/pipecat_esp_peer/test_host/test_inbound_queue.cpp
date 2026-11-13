#include "inbound_queue.h"
#include "socket_select_policy.h"
#include "handshake_policy.h"
#include <gtest/gtest.h>
#include <vector>
#include <algorithm>
using namespace pipecat::esp_peer;
TEST(HandshakePolicy, FatalErrorsStopRetriesAndCancellationIsBounded) {
    constexpr int read=-1,write=-2,verify=-3,timeout=-4;
    for(int value:{0,read,write,verify})EXPECT_FALSE(handshake_terminal(value,read,write,verify));
    EXPECT_TRUE(handshake_terminal(timeout,read,write,verify));
    EXPECT_TRUE(handshake_terminal(-100,read,write,verify));
    EXPECT_FALSE(handshake_cancelled(false,19999,20000));
    EXPECT_TRUE(handshake_cancelled(false,20000,20000));
    EXPECT_TRUE(handshake_cancelled(true,1,20000));
    EXPECT_FALSE(handshake_cancelled(false,99999,0));
}
TEST(HandshakePolicy, ConnectDeadlineExtendsOnlyHttpsAndAnInjectedFaultWinsOverBoth) {
    using std::chrono::milliseconds;using pipecat::esp_peer::connect_deadline;
    EXPECT_EQ(connect_deadline(milliseconds(20000),false,false),milliseconds(20000));
    EXPECT_EQ(connect_deadline(milliseconds(20000),true,false),milliseconds(40000));
    // The HTTPS floor also raises an explicitly shorter option; a longer one is kept.
    EXPECT_EQ(connect_deadline(milliseconds(5000),true,false),milliseconds(40000));
    EXPECT_EQ(connect_deadline(milliseconds(60000),true,false),milliseconds(60000));
    EXPECT_EQ(connect_deadline(milliseconds(5000),false,false),milliseconds(5000));
    for(bool https:{false,true})for(auto configured:{5000,20000,60000})
        EXPECT_EQ(connect_deadline(milliseconds(configured),https,true),milliseconds(3000));
}
TEST(Inbound, CopiesEntireBurstBeforeWorkerValidation) {
    std::vector<char> storage(64*64);InboundQueue q(storage.data(),storage.size(),64);
    for(int i=0;i<50;++i) {std::string text="token"+std::to_string(i);EXPECT_TRUE(q.push(text,i));text.assign("changed");}
    char out[64];size_t size;int64_t at;
    for(int i=0;i<50;++i) {ASSERT_TRUE(q.pop(out,sizeof out,size,at));EXPECT_EQ(std::string(out,size),"token"+std::to_string(i));EXPECT_EQ(at,i);}
    EXPECT_EQ(q.depth(),0u);
}
TEST(Inbound, BoundedOverflowAndInvalidBytesStayExplicit) {
    std::vector<char> storage(64*8);InboundQueue q(storage.data(),storage.size(),8);
    EXPECT_DEATH(q.push("",0),"size");
    EXPECT_DEATH(q.push("oversized",0),"size");
    for(int i=0;i<64;++i)EXPECT_TRUE(q.push(std::string_view("a\0b",3),i));
    EXPECT_FALSE(q.push("extra",0));
    char out[8];size_t size;int64_t at;
    EXPECT_THROW(q.pop(out,1,size,at),std::invalid_argument);EXPECT_EQ(q.depth(),64u);
    ASSERT_TRUE(q.pop(out,8,size,at));EXPECT_EQ(std::string(out,size),std::string("a\0b",3));
    EXPECT_TRUE(q.push("wrap",64));
}
TEST(SelectPolicy, MixedEmptyNullBoundsAndAllThreeSets) {
    fd_set read,write,error;FD_ZERO(&read);FD_ZERO(&write);FD_ZERO(&error);
    EXPECT_FALSE(sockets_only(12,&read,&write,&error,10));
    EXPECT_FALSE(sockets_only(12,nullptr,nullptr,nullptr,10));
    FD_SET(10,&read);FD_SET(11,&error);EXPECT_TRUE(sockets_only(12,&read,&write,&error,10));
    FD_SET(3,&write);EXPECT_FALSE(sockets_only(12,&read,&write,&error,10));
    EXPECT_FALSE(sockets_only(FD_SETSIZE+1,&read,nullptr,nullptr,10));
    EXPECT_FALSE(sockets_only(0,&read,nullptr,nullptr,10));
}

TEST(InboundQueue, WritingHeadBlocksFifoUntilPublication) {
    std::array<char,64*16> storage{};pipecat::esp_peer::InboundQueue q(storage.data(),storage.size(),16);
    auto first=q.reserve(3,10);auto second=q.reserve(3,20);
    std::memcpy(second.data,"two",3);q.publish(second);EXPECT_FALSE(q.claim());
    std::memcpy(first.data,"one",3);q.publish(first);
    auto read=q.claim();ASSERT_TRUE(read);EXPECT_EQ(std::string(read.data,read.size),"one");
    q.release(read);read=q.claim();EXPECT_EQ(std::string(read.data,read.size),"two");q.release(read);
}
TEST(InboundQueue, ReadingLeaseCannotBeReusedDuringOutsideLockCopy) {
    std::array<char,64*16> storage{};pipecat::esp_peer::InboundQueue q(storage.data(),storage.size(),16);
    for(unsigned i=0;i<64;++i)ASSERT_TRUE(q.push("held",i));
    auto read=q.claim();ASSERT_TRUE(read);EXPECT_FALSE(q.reserve(3,70));
    EXPECT_EQ(std::string(read.data,read.size),"held");q.release(read);
    auto fresh=q.reserve(3,70);ASSERT_TRUE(fresh);std::memcpy(fresh.data,"new",3);q.publish(fresh);
    EXPECT_EQ(q.depth(),64u);
}

#include <thread>
#include <mutex>
#include <atomic>
TEST(InboundQueue, TwoThreadLeasesAtCapacityKeepPayloadOwned) {
    std::vector<char> storage(64*64);InboundQueue q(storage.data(),storage.size(),64);
    std::mutex mutex;std::atomic<unsigned> errors{0},full{0};
    constexpr unsigned count=20000;
    // Begin full to guarantee producer overflow while consumer owns a lease.
    for(unsigned i=0;i<64;++i)q.push(std::string(64,char(i)),i);
    std::thread producer([&]{for(unsigned i=64;i<count;++i) {
        InboundQueue::Lease lease;
        do { {std::lock_guard<std::mutex> lock(mutex);lease=q.reserve(64,i);}
             if(!lease){++full;std::this_thread::yield();} }while(!lease);
        std::memset(lease.data,char(i),64);std::this_thread::yield();
        {std::lock_guard<std::mutex> lock(mutex);q.publish(lease);}
    }});
    std::thread consumer([&]{for(unsigned i=0;i<count;++i) {
        InboundQueue::Lease lease;
        do {{std::lock_guard<std::mutex> lock(mutex);lease=q.claim();}
            if(!lease)std::this_thread::yield();}while(!lease);
        std::this_thread::yield();char payload[64];std::memcpy(payload,lease.data,64);
        if(lease.at!=i || !std::all_of(payload,payload+64,[&](char c){return c==char(i);}))++errors;
        {std::lock_guard<std::mutex> lock(mutex);q.release(lease);}
    }});
    producer.join();consumer.join();EXPECT_EQ(errors,0u);EXPECT_EQ(q.depth(),0u);EXPECT_GT(full,0u);
}
