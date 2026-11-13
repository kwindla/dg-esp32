// Regression for the 2026-10-20 loss: an SCTP retransmit released ~20 held
// in-order messages in one owner pass and the per-pass work budget dropped five.
// The harness mirrors the transport data callback, owner-pass promotion and RX
// claim sequence exactly, on the same pure modules transport.cpp uses.
#include "inbound_queue.h"
#include "message_admission.h"
#include "msg_queue.h"
#include <gtest/gtest.h>
#include <memory>
#include <string>
#include <vector>
using namespace pipecat::esp_peer;
namespace {
struct Carryover {
    static constexpr size_t limit=MessageAdmission::wire_limit;
    std::vector<char> storage=std::vector<char>(InboundQueue::capacity*limit);
    InboundQueue inbound{storage.data(),storage.size(),limit};
    MessageWorkBudget work;
    unsigned received=0,deferred=0,overflow=0,max_held=0;
    uint64_t now=1000;
    // transport.cpp Session::data, from the reserve onward.
    void callback(const char* data,size_t size) {
        ++received;
        auto lease=inbound.reserve(size,int64_t(now/1000));
        if(!lease){++overflow;return;}
        std::memcpy(lease.data,data,size);
        const bool defer=inbound.held() || !work.admit(size,now);
        inbound.publish(lease,defer);
        if(defer){++deferred;max_held=std::max(max_held,unsigned(inbound.held()));}
    }
    // transport.cpp owner_run, top of each pass.
    void pass(uint64_t advance_us=1000) {
        now+=advance_us;work.begin();
        if(inbound.held())inbound.promote([&](size_t bytes){return work.admit(bytes,now);});
    }
    // transport.cpp rx_run claim loop (unbounded batch for the test).
    std::vector<std::string> drain() {
        std::vector<std::string> out;
        for(;;) {auto lease=inbound.claim();if(!lease)break;
            out.emplace_back(lease.data,lease.size);inbound.release(lease);}
        return out;
    }
};
std::string message(unsigned i,size_t size=150) {
    std::string text="{\"type\":\"bot-output\",\"id\":"+std::to_string(i)+",\"pad\":\"";
    text.append(size-text.size()-2,'x');return text+"\"}";
}
// Delivers every message from a single reused native buffer, which is
// overwritten after each callback as SCTP reuses its reassembly memory.
void burst(Carryover& c,unsigned first,unsigned count,size_t size=150) {
    std::vector<char> native(Carryover::limit);
    for(unsigned i=first;i<first+count;++i) {
        auto text=message(i,size);std::memcpy(native.data(),text.data(),text.size());
        c.callback(native.data(),text.size());std::fill(native.begin(),native.end(),'#');
    }
}
// Runs passes until every accepted message has been consumed, bounded.
std::vector<std::string> deliver(Carryover& c,unsigned max_passes=1000) {
    auto out=c.drain();
    for(unsigned p=0;p<max_passes && (c.inbound.depth() || c.inbound.held());++p) {
        c.pass();auto more=c.drain();out.insert(out.end(),more.begin(),more.end());
    }
    return out;
}
void expect_ordered(const std::vector<std::string>& out,unsigned first,unsigned count,size_t size=150) {
    ASSERT_EQ(out.size(),count);
    for(unsigned i=0;i<count;++i)EXPECT_EQ(out[i],message(first+i,size)) << i;
}
}
TEST(MessageCarryover, TwentyMessageRetransmitBurstIsDeliveredOrderedExactlyOnce) {
    Carryover c;c.pass();
    burst(c,0,20);
    EXPECT_EQ(c.deferred,4u);EXPECT_EQ(c.inbound.held(),4u);EXPECT_EQ(c.overflow,0u);
    // The first pass's 16 are consumable immediately; the held tail is not.
    auto first=c.drain();EXPECT_EQ(first.size(),16u);
    c.pass();auto rest=c.drain();
    first.insert(first.end(),rest.begin(),rest.end());
    expect_ordered(first,0,20);EXPECT_EQ(c.inbound.depth(),0u);EXPECT_EQ(c.inbound.held(),0u);
}
TEST(MessageCarryover, SixtyFourMessageBurstFillsDeclaredCapacityWithoutLoss) {
    Carryover c;c.pass();
    burst(c,0,64);
    EXPECT_EQ(c.overflow,0u);EXPECT_EQ(c.deferred,48u);EXPECT_EQ(c.max_held,48u);
    // Each later pass promotes at most the per-pass message limit.
    c.drain();c.pass();EXPECT_GE(c.inbound.held(),48u-MessageWorkBudget::message_limit);
    auto out=deliver(c);
    EXPECT_EQ(out.size()+16,64u);EXPECT_EQ(out.front(),message(16));EXPECT_EQ(out.back(),message(63));
}
TEST(MessageCarryover, PerPassPromotionNeverExceedsWorkBudget) {
    Carryover c;c.pass();burst(c,0,64,300);c.drain();
    for(unsigned p=0;c.inbound.held() && p<1000;++p) {
        const auto before=c.inbound.held();c.pass();
        EXPECT_LE(before-c.inbound.held(),MessageWorkBudget::message_limit);
        EXPECT_LE(c.work.bytes,MessageWorkBudget::burst);
        c.drain();
    }
    EXPECT_EQ(c.inbound.held(),0u);
}
TEST(MessageCarryover, ByteBudgetExhaustionHoldsLargeWorkAndKeepsSmallBehindIt) {
    Carryover c;c.pass();
    burst(c,0,3,2048);
    EXPECT_EQ(c.inbound.held(),1u);
    // A compact control-sized message would fit the small lane, but must not
    // overtake the carried-over large message.
    burst(c,3,1,100);EXPECT_EQ(c.inbound.held(),2u);
    auto out=c.drain();ASSERT_EQ(out.size(),2u);
    c.pass(1000);EXPECT_EQ(c.inbound.held(),2u) << "2 KiB credit needs ~15.6 ms to refill";
    EXPECT_TRUE(c.drain().empty());
    c.pass(16000);EXPECT_EQ(c.inbound.held(),0u);
    auto rest=c.drain();ASSERT_EQ(rest.size(),2u);
    EXPECT_EQ(rest[0],message(2,2048));EXPECT_EQ(rest[1],message(3,100));
}
TEST(MessageCarryover, BeyondCapacityOverflowIsExplicitAndAcceptedWorkSurvives) {
    Carryover c;c.pass();
    burst(c,0,70);   // No consumer progress during the burst.
    EXPECT_EQ(c.received,70u);EXPECT_EQ(c.overflow,6u);EXPECT_EQ(c.inbound.depth(),64u);
    auto out=deliver(c);
    expect_ordered(out,0,64);
    EXPECT_EQ(out.size()+c.overflow,size_t(c.received)) << "every message is delivered or counted";
    // Capacity is restored once drained.
    c.pass();burst(c,100,1);EXPECT_EQ(c.overflow,6u);expect_ordered(deliver(c),100,1);
}
TEST(MessageCarryover, OwnedCopySurvivesFreedNativeBuffer) {
    Carryover c;c.pass();
    for(unsigned i=0;i<24;++i) {
        auto text=message(i);
        auto native=std::make_unique<char[]>(text.size());
        std::memcpy(native.get(),text.data(),text.size());
        c.callback(native.get(),text.size());
        std::memset(native.get(),0,text.size());   // freed right after the callback
    }
    EXPECT_EQ(c.inbound.held(),8u);
    expect_ordered(deliver(c),0,24);
}
TEST(MessageCarryover, DispatchQueueBackpressureLeavesWorkInboundInsteadOfDropping) {
    Carryover c;c.pass();burst(c,0,50);
    std::vector<char> dispatch_storage(MessageQueue::capacity*Carryover::limit);
    MessageQueue messages(dispatch_storage.data(),dispatch_storage.size(),Carryover::limit);
    // The SDK accepts nothing: the RX worker only claims while can_push().
    for(unsigned p=0;p<20;++p) {
        c.pass();
        while(messages.can_push()) {auto lease=c.inbound.claim();if(!lease)break;
            EXPECT_EQ(messages.push({lease.data,lease.size},0),MessageQueue::Result::Queued);c.inbound.release(lease);}
    }
    EXPECT_EQ(messages.depth(),MessageQueue::capacity-1);EXPECT_EQ(c.inbound.depth(),50u-31u);
    EXPECT_EQ(messages.counters().overflow,0u);
    // The SDK resumes: one pop then one claim per pass, as in rx_run.
    std::vector<std::string> out;std::vector<char> wire(Carryover::limit);
    for(unsigned p=0;p<200 && (messages.depth() || c.inbound.depth());++p) {
        c.pass();
        if(messages.can_push()) if(auto lease=c.inbound.claim()) {
            ASSERT_EQ(messages.push({lease.data,lease.size},0),MessageQueue::Result::Queued);c.inbound.release(lease);}
        size_t size;int64_t at;if(messages.pop(wire.data(),wire.size(),size,at))out.emplace_back(wire.data(),size);
    }
    expect_ordered(out,0,50);
}
TEST(MessageCarryover, HeldRunMustStayContiguous) {
    std::vector<char> storage(InboundQueue::capacity*16);InboundQueue q(storage.data(),storage.size(),16);
    auto first=q.reserve(3,1),second=q.reserve(3,2);
    EXPECT_TRUE(q.publish(second,true));
    EXPECT_THROW(q.publish(first,false),std::logic_error);
}
TEST(MessageCarryover, ReadyAfterHeldIsForcedToHoldAndPromotionStopsAtBudget) {
    std::vector<char> storage(InboundQueue::capacity*16);InboundQueue q(storage.data(),storage.size(),16);
    EXPECT_FALSE(q.publish(q.reserve(1,0),false));
    EXPECT_TRUE(q.publish(q.reserve(2,0),true));
    EXPECT_TRUE(q.publish(q.reserve(3,0),false));
    EXPECT_EQ(q.held(),2u);
    std::vector<size_t> offered;
    EXPECT_EQ(q.promote([&](size_t n){offered.push_back(n);return n<3;}),1u);
    EXPECT_EQ(offered,(std::vector<size_t>{2,3}));EXPECT_EQ(q.held(),1u);
    EXPECT_EQ(q.promote([](size_t){return true;}),1u);EXPECT_EQ(q.held(),0u);
}
