#include "wire_contract.h"
#include "uplink_intervals.h"
#include <gtest/gtest.h>
using namespace pipecat::esp_peer;
TEST(WireSanitizer, StartHasOnlyTheCapabilityBodyNeverArbitraryCredentialFields) {
    nlohmann::json request={{"transport","webrtc"},{"body",{{"audio",{{"v",1},{"aec",false},{"ref_gain_db",0},{"guard_ms",0}}}}}};
    EXPECT_TRUE(wire_start_safe(request));request["body"]["token"]="PRIVATE";EXPECT_FALSE(wire_start_safe(request));
    const auto names=wire_start_keys(request);
    EXPECT_EQ(names,nlohmann::json({"body","transport"}));
    EXPECT_EQ(names.dump().find("PRIVATE"),std::string::npos);
    EXPECT_EQ(names.dump().find("token"),std::string::npos);
    EXPECT_EQ(wire_start_keys({{"token","PRIVATE"},{"endpoint","PRIVATE"}}),nlohmann::json({"endpoint","token"}));
    EXPECT_TRUE(wire_start_keys(nlohmann::json::array()).empty());
    request["body"].erase("token");request["body"]["audio"]["aec"]="false";EXPECT_FALSE(wire_start_safe(request));
}
TEST(WireSanitizer, OnlyMediaAndApprovedAttributesLeaveSdp) {
    const auto out=wire_sdp("v=0\r\no=user secret\r\na=rtpmap:9 outside-media\r\n"
        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=ice-ufrag:PRIVATE\r\na=ice-pwd:PRIVATE\r\n"
        "a=fingerprint:PRIVATE\r\na=candidate:PRIVATE\r\na=rtpmap:111 opus/48000/2\r\n"
        "a=fmtp:111 minptime=10\r\na=ptime:20\r\na=rtpmapx:PRIVATE\r\n"
        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\na=sctp-port:5000\na=max-message-size:16384");
    EXPECT_EQ(out,nlohmann::json({"m=audio 9 UDP/TLS/RTP/SAVPF 111","a=rtpmap:111 opus/48000/2",
        "a=fmtp:111 minptime=10","a=ptime:20","m=application 9 UDP/DTLS/SCTP webrtc-datachannel",
        "a=sctp-port:5000","a=max-message-size:16384"}));
    EXPECT_EQ(out.dump().find("PRIVATE"),std::string::npos);
}
TEST(WireSanitizer, ReadyPayloadHasNoGeneratedEnvelopeIdAndSchemasHaveNoDynamicValues) {
    nlohmann::json data={{"about",{{"library","test"}}},{"version","1.0.0"}};
    auto ready=wire_message_record({{"id","dynamic-envelope"},{"type","client-ready"},{"data",data}});
    EXPECT_EQ(ready.first,"client_ready");EXPECT_EQ(ready.second,data);
    EXPECT_EQ(wire_message_kind("client-ready",data),WireMessage::Ready);
    nlohmann::json body={{"v",1},{"epoch",999u},{"next_seq",27u},{"wire_bytes",1023},{"max_ops",8}};
    auto display=wire_message_record({{"type","client-message"},{"data",{{"t","display-ready"},{"d",body}}}});
    EXPECT_EQ(display.first,"msg=display-ready");EXPECT_EQ(display.second["schema"]["epoch"],"integer");
    EXPECT_EQ(display.second["values"],nlohmann::json({{"v",1},{"wire_bytes",1023},{"max_ops",8}}));
    auto result=wire_message_record({{"type","client-message"},{"data",{{"t","display-result"},{"d",{{"id","known"},{"wait_us",123}}}}}});
    EXPECT_EQ(result.second["values"],nlohmann::json({{"id","known"}}));
    EXPECT_EQ(wire_message_kind("client-message",{{"t","other"}}),WireMessage::None);
}
TEST(UplinkIntervals, WarmupAttemptsFailuresExactMaxAndOverflowAreDistinct) {
    UplinkIntervals sample(7);sample.submit(100,false);sample.submit(1000,true);
    sample.submit(2000000,true); // Still in warm-up.
    sample.submit(2001000,true);sample.submit(2010000,false);
    sample.submit(2021000,true);sample.submit(2221123,true);
    UplinkSnapshot out;ASSERT_TRUE(sample.claim(2231123,out));
    EXPECT_EQ(out.generation,7u);EXPECT_EQ(out.start_us,2001000);EXPECT_EQ(out.end_us,2231123);
    EXPECT_EQ(out.attempts,4u);EXPECT_EQ(out.ok,3u);EXPECT_EQ(out.n,2u);
    EXPECT_EQ(out.hist[20],1u);EXPECT_EQ(out.hist[200],1u);EXPECT_EQ(out.max_us,200123u);
    EXPECT_EQ(out.last_ok_age_us,10000);
    sample.submit(9000000,true);EXPECT_FALSE(sample.claim(10000000,out));
    UplinkSnapshot saved;sample.copy(saved);EXPECT_EQ(saved.end_us,2231123);
}
TEST(UplinkIntervals, FirstFreezePreservesTrailingGapAndNextGenerationStartsFresh) {
    UplinkIntervals first(1);first.submit(100,true);first.submit(2000100,true);first.submit(2020100,true);
    first.freeze(4020100);first.submit(5020100,true);
    UplinkSnapshot previous;ASSERT_TRUE(first.claim(6020100,previous));
    EXPECT_EQ(previous.last_ok_age_us,2000000);EXPECT_EQ(previous.end_us,4020100);
    UplinkIntervals next(3);next.submit(7000000,true);UplinkSnapshot current;next.claim(9000000,current);
    EXPECT_EQ(previous.generation,1u);EXPECT_EQ(previous.ok,2u);EXPECT_EQ(current.generation,3u);
}
