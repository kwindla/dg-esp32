#include "receive_budget_policy.h"
#include "message_admission.h"
#include "inbound_queue.h"
#include "msg_queue.h"
#include <string>
#include "rtp_ingress.h"
#include "playout.h"
#include <gtest/gtest.h>
#include <vector>
using namespace pipecat::esp_peer;
TEST(ReceiveBudget, ReadCountsAndBytesEndPassEvenWhenTimeStandsStill) {
    for(bool packets:{false,true}) {
        ReceiveBudget b;b.begin(true,true,1000);
        EXPECT_EQ(b.deadline_us,0);
        for(unsigned n=0;n<(packets?4:1);++n)b.received(packets?1:4096);
        EXPECT_TRUE(b.exhausted(1000));
        fd_set read;FD_ZERO(&read);FD_SET(3,&read);timeval bounded{},*timeout=nullptr;
        EXPECT_EQ(bound_receive_select(b.deadline_us,1000,&read,nullptr,nullptr,timeout,bounded),0);
        EXPECT_FALSE(FD_ISSET(3,&read));
        b.finish();b.begin(true,true,2000);EXPECT_EQ(b.bytes,0u);EXPECT_EQ(b.packets,0u);
    }
}
TEST(ReceiveBudget, BusyTimeExpiresButWriteAndSetupArePreserved) {
    ReceiveBudget b;b.begin(true,true,1000);b.received(1);
    fd_set read,write;FD_ZERO(&read);FD_ZERO(&write);timeval bounded{},*timeout=nullptr;
    EXPECT_EQ(bound_receive_select(b.deadline_us,5001,&read,&write,nullptr,timeout,bounded),-1);
    EXPECT_EQ(bound_receive_select(b.deadline_us,5001,&read,nullptr,nullptr,timeout,bounded),0);
    b.begin(false,true,1000);b.received(9000);EXPECT_EQ(b.deadline_us,0);
}
namespace {
AudioTrack prechat_track() {
    AudioTrack track;track.opus[109]=true;track.ssrc=7;track.generation=1;return track;
}
std::vector<uint8_t> media_packet(unsigned seq,bool extension=false) {
    std::vector<uint8_t> p={uint8_t(extension?0x90:0x80),109,uint8_t(seq>>8),uint8_t(seq),0,0,0,0,0,0,0,7};
    const auto timestamp=960*(seq+1);
    for(unsigned j=0;j<4;++j)p[4+j]=uint8_t(timestamp>>(24-j*8));
    if(extension) {p.insert(p.end(),{0xbe,0xde,0,56});p.resize(12+4+56*4);}
    p.push_back(0x48);p.resize(extension?1440:14,42);
    return p;
}
}
TEST(ReceiveBudget, SustainedAuthorizedPrechatMediaIsBoundedInCurrentPass) {
    for(bool byte_exhaustion:{false,true})for(bool after_read:{false,true}) {
        ReceiveBudget budget;budget.begin(false,after_read,1000);
        const auto track=prechat_track();Playout::Storage storage{};Playout playout(storage);
        playout.authorize(track.ssrc,track.generation);
        unsigned reads=0;
        for(unsigned i=0;i<1000 && !budget.exhausted(1000+i*100);++i) {
            auto p=media_packet(i,byte_exhaustion);budget.received(p.size()+10);++reads;
            RtpAudioView packet;
            const auto admission=admit_audio_rtp(p.data(),p.size(),track,false,false,packet);
            ASSERT_EQ(admission.error,IngressError::None);EXPECT_TRUE(admission.before_chat);
            EXPECT_EQ(budget.admitted_media(),i==0);
            ASSERT_TRUE(playout.insert(packet,1000+i*100));
        }
        EXPECT_EQ(reads,byte_exhaustion?3u:4u);
        EXPECT_TRUE(budget.exhausted(1500));
        auto counters=playout.snapshot(1500).counters;
        EXPECT_EQ(counters.accepted,reads);
        for(auto rejects:counters.rejected)EXPECT_EQ(rejects,0u);
        fd_set read,error;FD_ZERO(&read);FD_ZERO(&error);FD_SET(3,&read);FD_SET(3,&error);
        timeval bounded{},*timeout=nullptr;
        EXPECT_EQ(bound_receive_select(budget.deadline_us,1500,&read,nullptr,&error,timeout,bounded),0);
        EXPECT_FALSE(FD_ISSET(3,&read));EXPECT_FALSE(FD_ISSET(3,&error));
    }
}
TEST(ReceiveBudget, PrechatTimeBoundCoversTransitionAndCannotBeRenewedByMedia) {
    ReceiveBudget b;b.begin(false,true,1000);b.received(100);
    EXPECT_TRUE(b.admitted_media());EXPECT_EQ(b.deadline_us,5000);
    EXPECT_FALSE(b.exhausted(4999));EXPECT_TRUE(b.exhausted(5000));
    // A later callback cannot start a fresh 4ms window inside the same pass.
    EXPECT_FALSE(b.admitted_media());EXPECT_EQ(b.deadline_us,5000);
    EXPECT_TRUE(b.exhausted(5001));
    fd_set read,write;FD_ZERO(&read);FD_ZERO(&write);FD_SET(3,&read);FD_SET(3,&write);
    timeval bounded{},*timeout=nullptr;
    EXPECT_EQ(bound_receive_select(b.deadline_us,5001,&read,&write,nullptr,timeout,bounded),-1);
    EXPECT_TRUE(FD_ISSET(3,&read));EXPECT_TRUE(FD_ISSET(3,&write));
}
TEST(ReceiveBudget, SetupRemainsExemptUntilValidMediaThenPriorReadsAndTimeCount) {
    for(bool large_setup:{false,true}) {
        ReceiveBudget b;b.begin(false,true,1000);
        // ICE/DTLS-only setup can exceed all three bounds without being cut.
        for(unsigned i=0;i<8;++i)b.received(large_setup?1024:1);
        EXPECT_FALSE(b.exhausted(100000));EXPECT_EQ(b.deadline_us,0);
        // The first valid media callback immediately bounds this same pass.
        b.received(100);EXPECT_TRUE(b.admitted_media());
        EXPECT_TRUE(b.exhausted(100001));EXPECT_EQ(b.packets,9u);
    }
    ReceiveBudget slow;slow.begin(false,true,1000);slow.received(10);
    EXPECT_FALSE(slow.exhausted(100000));slow.admitted_media();
    EXPECT_EQ(slow.packets,1u);EXPECT_EQ(slow.bytes,10u);
    EXPECT_TRUE(slow.exhausted(100000)); // Time alone, not packet/byte count.
}
TEST(ReceiveBudget, PrechatActivationPersistsAcrossPassesButNotAcrossSessions) {
    ReceiveBudget b;b.begin(false,true,1000);b.received(20);b.admitted_media();b.finish();
    EXPECT_EQ(b.deadline_us,0);EXPECT_EQ(b.pass_deadline_us,0);
    b.begin(false,true,7000);EXPECT_TRUE(b.awaiting_read);EXPECT_EQ(b.packets,0u);EXPECT_EQ(b.bytes,0u);
    EXPECT_EQ(b.pass_deadline_us,11000);EXPECT_EQ(b.deadline_us,0);
    b.received(20);EXPECT_EQ(b.deadline_us,11000);EXPECT_TRUE(b.exhausted(11000));
    ReceiveBudget fresh;fresh.begin(false,true,12000);fresh.received(9000);
    EXPECT_FALSE(fresh.media_active);EXPECT_FALSE(fresh.exhausted(999999));
}
TEST(MessageAdmission, BoundDenseTreesBeforeLexerAndAccountForEscapes) {
    EXPECT_GT(MessageAdmission::nodes("["+std::string(100,',')+"]"),MessageAdmission::node_limit);
    EXPECT_LT(MessageAdmission::nodes(R"({"text":"{[,:\"}]"})"),10u);
    auto r=MessageAdmission::reserve(1024,16);
    EXPECT_TRUE(MessageAdmission::memory(81920+r,40000,1024,16));
    EXPECT_FALSE(MessageAdmission::memory(81920+r-1,40000,1024,16));
    EXPECT_FALSE(MessageAdmission::memory(200000,32768,1024,16));
}
TEST(MessageAdmission, SustainedByteBudgetDoesNotRefillWithEveryPass) {
    MessageWorkBudget b;
    for(int i=0;i<4;++i)EXPECT_TRUE(b.admit(1024,1000));
    b.begin();EXPECT_FALSE(b.admit(1024,1000));
    EXPECT_FALSE(b.admit(1024,8000));
    EXPECT_TRUE(b.admit(1024,9000));
    b.begin();EXPECT_TRUE(b.admit(2048,1000000));EXPECT_TRUE(b.admit(2048,1000000));
    b.begin();EXPECT_FALSE(b.admit(1025,1000000));
    for(int i=0;i<4;++i)EXPECT_TRUE(b.admit(256,1000000));
    b.begin();EXPECT_FALSE(b.admit(256,1000000));
}

TEST(MessageAdmission, RealReplyAndInterruptionBurstsFitOneIteration) {
    MessageWorkBudget b;
    for(auto size:{120u,90u,120u,100u,675u,675u,200u,160u})
        EXPECT_TRUE(b.admit(size,1000));
    b.begin();
    for(int i=0;i<7;++i)EXPECT_TRUE(b.admit(100,21000));
}
TEST(MessageAdmission, MessageAndByteCapsRemainBounded) {
    MessageWorkBudget b;
    for(unsigned i=0;i<16;++i)EXPECT_TRUE(b.admit(1,1000));
    EXPECT_FALSE(b.admit(1,1000));
    b.begin();EXPECT_TRUE(b.admit(2048,1000000));EXPECT_TRUE(b.admit(2048,1000000));
    EXPECT_FALSE(b.admit(1,1000000));
    b.begin();EXPECT_FALSE(b.admit(4097,2000000));
    EXPECT_FALSE(b.admit(size_t(-1),2000000));
}

TEST(MessageAdmission, NominalCombinedMetricsShapeRetainsHeapGuard) {
    const auto message=R"JSON({"label":"rtvi-ai","type":"metrics","data":{"ttfb":[{"processor":"DeepgramFluxSTTService#0","value":0.0},{"processor":"OpenAILLMService#0","value":0.0},{"processor":"DeepgramFluxTTSService#0","value":0.0}],"processing":[{"processor":"DeepgramFluxSTTService#0","value":0.0},{"processor":"OpenAILLMService#0","value":0.0},{"processor":"DeepgramFluxTTSService#0","value":0.0}]}})JSON";
    const auto n=MessageAdmission::nodes(message);
    EXPECT_EQ(n,66u);
    EXPECT_LE(n,MessageAdmission::node_limit);
    EXPECT_FALSE(MessageAdmission::memory(81920,47104,std::string_view(message).size(),n));
    EXPECT_GT(MessageAdmission::nodes("["+std::string(100,',')+"]"),MessageAdmission::node_limit);
}

TEST(MessageAdmission, InboundSlotsAreSizedByTheWireLimitNotTheOutboundMaximum) {
    static_assert(MessageAdmission::slot_bytes(16384)==MessageAdmission::wire_limit);
    static_assert(MessageAdmission::slot_bytes(1024)==1024);
    const size_t slot=MessageAdmission::slot_bytes(16384);
    std::vector<char> inbound_storage(InboundQueue::capacity*slot),dispatch_storage(MessageQueue::capacity*slot);
    EXPECT_EQ(inbound_storage.size()+dispatch_storage.size(),196608u); // 1,572,864 bytes at 16 KiB slots
    InboundQueue inbound(inbound_storage.data(),inbound_storage.size(),slot);
    MessageQueue dispatch(dispatch_storage.data(),dispatch_storage.size(),slot);
    char out[MessageAdmission::wire_limit];size_t size=0;int64_t at=0;
    EXPECT_TRUE(inbound.push(std::string(slot,'x'),1));
    ASSERT_TRUE(inbound.pop(out,sizeof out,size,at));EXPECT_EQ(size,slot);
    const std::string largest="{\"k\":\""+std::string(slot-8,'y')+"\"}";ASSERT_EQ(largest.size(),slot);
    EXPECT_EQ(dispatch.push(largest,1),MessageQueue::Result::Queued);
    ASSERT_TRUE(dispatch.pop(out,sizeof out,size,at));EXPECT_EQ(size,slot);
    const std::string oversize="{\"k\":\""+std::string(slot-7,'y')+"\"}";ASSERT_EQ(oversize.size(),slot+1);
    EXPECT_EQ(dispatch.push(oversize,2),MessageQueue::Result::Oversize);
}
