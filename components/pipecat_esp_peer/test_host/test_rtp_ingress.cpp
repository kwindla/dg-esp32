#include "rtp_ingress.h"
#include "playout.h"
#include <gtest/gtest.h>
#include <vector>
using namespace pipecat::esp_peer;
static AudioTrack track() {auto a=audio_track("v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 109 0\r\na=rtpmap:109 opus/48000/2\r\na=ssrc:7 cname:foo\r\nm=video 9 UDP/TLS/RTP/SAVPF 111\r\na=rtpmap:111 opus/48000/2\r\na=ssrc:8 cname:other\r\n",1);return a;}
static std::vector<uint8_t> packet() {return {0x80,109,0,10,0,0,3,192,0,0,0,7,0x48,42};}
TEST(RtpIngress, NegotiatedTrackNotHardcodedPayloadType) {
    auto a=track();EXPECT_TRUE(a.opus[109]);EXPECT_FALSE(a.opus[111]);EXPECT_EQ(a.ssrc,7u);
    auto p=packet();RtpAudioView v;EXPECT_EQ(parse_audio_rtp(p.data(),p.size(),a,v),IngressError::None);
    EXPECT_EQ(v.sequence,10u);EXPECT_EQ(v.timestamp,960u);EXPECT_EQ(v.ssrc,7u);EXPECT_EQ(v.size,2u);
    EXPECT_EQ(v.payload,p.data()+12);p[1]=111;EXPECT_EQ(parse_audio_rtp(p.data(),p.size(),a,v),IngressError::PayloadType);
    p[1]=109;p[11]=8;EXPECT_EQ(parse_audio_rtp(p.data(),p.size(),a,v),IngressError::Source);
}
TEST(RtpIngress, OwnerHeldTrackClearsPreviousNegotiation) {
    auto a=track();
    audio_track("m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=rtpmap:111 opus/48000/2\r\na=ssrc:9 cname:new\r\n",2,a);
    EXPECT_FALSE(a.opus[109]);EXPECT_TRUE(a.opus[111]);
    EXPECT_EQ(a.ssrc,9u);EXPECT_EQ(a.generation,2u);
    audio_track("m=audio 9 UDP/TLS/RTP/SAVPF 109\r\na=rtpmap:109 opus/48000/2\r\n",3,a);
    EXPECT_FALSE(a.opus[111]);EXPECT_TRUE(a.opus[109]);
    EXPECT_EQ(a.ssrc,0u);EXPECT_EQ(a.generation,0u);
}
TEST(RtpIngress, CsrcExtensionPaddingAndIdentityBorrow) {
    auto p=packet();p[0]=0xb1;p.insert(p.begin()+12,{0,0,0,99,0xbe,0xde,0,1,0,0,0,0});
    p.insert(p.end(),{0,0,0,4});auto original=p;RtpAudioView v;
    EXPECT_EQ(parse_audio_rtp(p.data(),p.size(),track(),v),IngressError::None);
    EXPECT_EQ(v.payload,p.data()+24);EXPECT_EQ(v.size,2u);EXPECT_EQ(p,original);
    p.back()=0;EXPECT_EQ(parse_audio_rtp(p.data(),p.size(),track(),v),IngressError::Malformed);
    p.back()=4;p[19]=50;EXPECT_EQ(parse_audio_rtp(p.data(),p.size(),track(),v),IngressError::Malformed);
}
TEST(RtpIngress, InvalidExtentsDurationsAndOpusFraming) {
    RtpAudioView v;auto p=packet();auto a=track();
    for(unsigned n=0;n<12;++n)EXPECT_EQ(parse_audio_rtp(p.data(),n,a,v),IngressError::Malformed);
    p[0]=0x8f;EXPECT_EQ(parse_audio_rtp(p.data(),p.size(),a,v),IngressError::Malformed);
    p=packet();p[12]=0x40;EXPECT_EQ(parse_audio_rtp(p.data(),p.size(),a,v),IngressError::Duration);
    EXPECT_TRUE(opus_20ms((const uint8_t*)"\x48\x42",2));
    EXPECT_TRUE(opus_20ms((const uint8_t*)"\x41\x42\x42",3));
    EXPECT_FALSE(opus_20ms((const uint8_t*)"\x41\x42",2));
    EXPECT_FALSE(opus_20ms((const uint8_t*)"\x43\x82\xff",3));
}

TEST(RtpIngress, MediaBeforeChatIsRenderedWithoutLossOrEditing) {
    // The reconnect failure rejected the first three packets solely because
    // chat had not opened yet. Exercise the production admission and playout
    // together, including the exact three-packet prefix. Neither RTP identity
    // nor payload can be changed just to make a startup counter disappear.
    for(unsigned early:{0u,1u,3u,6u}) {
        Playout::Storage storage{};Playout playout(storage);
        auto a=track();playout.authorize(a.ssrc,a.generation);
        bool chat_open=false;unsigned before_chat=0,decoded=0;
        for(unsigned i=0;i<200;++i) {
            if(i==early)chat_open=true;
            auto p=packet();p[2]=uint8_t((10+i)>>8);p[3]=uint8_t(10+i);
            uint32_t ts=960*(i+1);
            for(unsigned k=0;k<4;++k)p[4+k]=uint8_t(ts>>(24-8*k));
            const auto unchanged=p;RtpAudioView view;
            auto admission=admit_audio_rtp(p.data(),p.size(),a,false,chat_open,view);
            ASSERT_EQ(admission.error,IngressError::None);
            EXPECT_EQ(admission.before_chat,!chat_open);
            if(admission.before_chat)++before_chat;
            ASSERT_TRUE(playout.insert(view,i*20000));EXPECT_EQ(p,unchanged);
            dg::RenderTick tick;tick.valid=true;tick.device_epoch=1;
            tick.index=i;tick.deadline_us=i*20000;
            auto decision=playout.pull(tick,true);
            if(decision.kind==Playout::Kind::Decode) {
                EXPECT_EQ(decision.timestamp,960ll*(decoded+1));
                ASSERT_EQ(decision.size,2u);EXPECT_EQ(decision.payload[1],42);
                ++decoded;
            } else EXPECT_EQ(decision.kind,Playout::Kind::Prefill);
            playout.finish(decision,true);
        }
        EXPECT_EQ(before_chat,early);EXPECT_EQ(decoded,200u-Playout::target_frames); // one prefill decision per reserve frame
        auto counts=playout.snapshot(4000000).counters;
        EXPECT_EQ(counts.accepted,200u);EXPECT_EQ(counts.missing_slots,0u);
        EXPECT_EQ(counts.plc_calls,0u);EXPECT_EQ(counts.drift_drop,0u);
        EXPECT_EQ(counts.drift_insert,0u);EXPECT_EQ(counts.latency_trim,0u);
        for(auto rejected:counts.rejected)EXPECT_EQ(rejected,0u);
    }
}

TEST(RtpIngress, EarlyMediaStillRequiresLiveSignaledTrackAndValidPacket) {
    auto p=packet();auto a=track();RtpAudioView view;
    EXPECT_EQ(admit_audio_rtp(p.data(),p.size(),a,true,false,view).error,IngressError::Generation);
    EXPECT_EQ(admit_audio_rtp(p.data(),p.size(),AudioTrack{},false,false,view).error,IngressError::PayloadType);
    auto next=a;next.ssrc=9;next.generation=2;
    EXPECT_EQ(admit_audio_rtp(p.data(),p.size(),next,false,false,view).error,IngressError::Source);
    p[1]=111;EXPECT_EQ(admit_audio_rtp(p.data(),p.size(),a,false,false,view).error,IngressError::PayloadType);
    p=packet();p[12]=0x40;
    EXPECT_EQ(admit_audio_rtp(p.data(),p.size(),a,false,false,view).error,IngressError::Duration);
    EXPECT_EQ(admit_audio_rtp(p.data(),11,a,false,false,view).error,IngressError::Malformed);
}
