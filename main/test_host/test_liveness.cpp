#include <gtest/gtest.h>
#include <cstdio>
#include "liveness.h"
TEST(Liveness, ConsumesRejectedPingWithoutRoutingItToDisplayOrGenericServerLogging) {
    dg::DeviceLiveness l;l.ready(1,0);using Ping=dg::DeviceLiveness::Ping;
    EXPECT_EQ(l.receive("{\"t\":\"ping\",\"seq\":3}",false,100),Ping::Accepted);
    EXPECT_EQ(l.receive("{\"t\":\"ping\",\"seq\":3}",false,200),Ping::Rejected);
    EXPECT_EQ(l.receive("{\"t\":\"ping\",\"seq\":2}",false,200),Ping::Rejected);
    EXPECT_EQ(l.receive("{\"t\":\"ping\"}",false,200),Ping::Rejected);
    EXPECT_EQ(l.receive("{\"t\":\"display.fill\"}",false,200),Ping::NotPing);
    EXPECT_EQ(l.take(),(uint64_t(1)<<32)|3);EXPECT_EQ(l.pings,1u);
}
TEST(Liveness, PongAndDisplayWorkMustBelongToTheCurrentReadySession) {
    EXPECT_TRUE(dg::session_work_current(7,7,7,true));
    EXPECT_FALSE(dg::session_work_current(7,7,8,true));
    EXPECT_FALSE(dg::session_work_current(7,8,8,true));
    EXPECT_FALSE(dg::session_work_current(8,7,8,true));
    EXPECT_FALSE(dg::session_work_current(8,8,8,false));
}
TEST(Liveness, ValidPingMailboxCountersAndGeneration) {
    dg::DeviceLiveness l;l.ready(7,100);
    EXPECT_TRUE(l.ping("{\"t\":\"ping\",\"seq\":1}",false,1000));
    EXPECT_EQ(l.pings,1u);EXPECT_EQ(l.take(),(uint64_t(7)<<32)|1);EXPECT_EQ(l.take(),0u);
    ++l.pongs;EXPECT_EQ(l.pongs,1u);
    l.ready(8,2000);EXPECT_EQ(l.pings,0u);EXPECT_EQ(l.pongs,0u);EXPECT_EQ(l.take(),0u);
}
TEST(Liveness, RejectMalformedAndTruncated) {
    dg::DeviceLiveness l;l.ready(1,0);
    for(auto s:{"{}","[]","{","{\"t\":\"ping\",\"seq\":true}","{\"t\":\"ping\",\"seq\":-1}","{\"t\":\"ping\",\"seq\":0}","{\"t\":\"ping\",\"seq\":4294967296}"})EXPECT_FALSE(l.ping(s,false,1));
    EXPECT_FALSE(l.ping("{\"t\":\"ping\",\"seq\":1}",true,1));EXPECT_EQ(l.pings,0u);
}
TEST(Liveness, RejectReplayAndReorderingWithoutReplacingMailboxOrRefreshingDeadline) {
    dg::DeviceLiveness l;l.ready(7,0);
    EXPECT_TRUE(l.ping("{\"t\":\"ping\",\"seq\":3}",false,100));
    EXPECT_FALSE(l.ping("{\"t\":\"ping\",\"seq\":3}",false,14000000));
    EXPECT_FALSE(l.ping("{\"t\":\"ping\",\"seq\":2}",false,14000000));
    EXPECT_EQ(l.take(),(uint64_t(7)<<32)|3);EXPECT_EQ(l.pings,1u);
    EXPECT_TRUE(l.missing(15000100));
    EXPECT_TRUE(l.ping("{\"t\":\"ping\",\"seq\":4}",false,16000000));
    l.ready(8,17000000);EXPECT_EQ(l.take(),0u);
    EXPECT_TRUE(l.ping("{\"t\":\"ping\",\"seq\":1}",false,17000100));
    EXPECT_EQ(l.take(),(uint64_t(8)<<32)|1);
}
TEST(Liveness, OneMissingWarningPerConnectionAndPingRefreshesDeadline) {
    dg::DeviceLiveness l;l.ready(1,0);EXPECT_FALSE(l.missing(14999999));
    EXPECT_TRUE(l.ping("{\"t\":\"ping\",\"seq\":1}",false,10000000));
    EXPECT_FALSE(l.missing(24999999));EXPECT_TRUE(l.missing(25000000));EXPECT_FALSE(l.missing(90000000));
    EXPECT_EQ(l.timeouts,1u);l.ready(2,90000000);EXPECT_FALSE(l.missing(90000000));
}
TEST(Liveness, LateFirstPingArmsLossDetectionAfterAnUnarmedWarning) {
    dg::DeviceLiveness l;l.ready(1,0);
    EXPECT_TRUE(l.missing(15000000));EXPECT_EQ(l.pings,0u);EXPECT_EQ(l.timeouts,1u);
    EXPECT_FALSE(l.missing(30000000));  // never-pinging bot: one warning, no spam
    EXPECT_TRUE(l.ping("{\"t\":\"ping\",\"seq\":1}",false,31000000));
    EXPECT_FALSE(l.missing(45999999));
    EXPECT_TRUE(l.missing(46000000));EXPECT_EQ(l.pings,1u);EXPECT_EQ(l.timeouts,2u);
    EXPECT_FALSE(l.missing(90000000));
}
TEST(Liveness, SolProbeScheduleWarnThenFirstPingThenSilence) {
    dg::DeviceLiveness l;l.ready(1,0);
    EXPECT_TRUE(l.missing(15000000));
    EXPECT_TRUE(l.ping("{\"t\":\"ping\",\"seq\":1}",false,16000000));
    EXPECT_TRUE(l.missing(31000000));EXPECT_EQ(l.pings,1u);
}
namespace {
using Silence=dg::DeviceLiveness::Silence;
const char* seq(int n) {
    static char text[40];std::snprintf(text,sizeof text,"{\"t\":\"ping\",\"seq\":%d}",n);return text;
}
}
TEST(Liveness, ArmedSilenceLosesTheSessionOnce) {
    dg::DeviceLiveness l;l.ready(1,0);
    EXPECT_TRUE(l.ping(seq(1),false,500000));
    EXPECT_EQ(l.silence(15499999),Silence::None);
    EXPECT_EQ(l.silence(15500000),Silence::Lost);
    EXPECT_EQ(l.silence(15500001),Silence::None);EXPECT_EQ(l.silence(600000000),Silence::None);
    EXPECT_EQ(l.timeouts,1u);
}
TEST(Liveness, BotThatNeverPingsIsWarnedOnceAndKeepsItsSession) {
    dg::DeviceLiveness l;l.ready(1,0);
    EXPECT_EQ(l.silence(14999999),Silence::None);
    EXPECT_EQ(l.silence(15000000),Silence::Unarmed);
    for(int64_t now=16000000;now<=600000000;now+=1000000)EXPECT_EQ(l.silence(now),Silence::None);
    EXPECT_EQ(l.timeouts,1u);EXPECT_EQ(l.pings,0u);
}
TEST(Liveness, LateFirstPingArmsTheNextSilence) {
    dg::DeviceLiveness l;l.ready(1,0);
    EXPECT_EQ(l.silence(15000000),Silence::Unarmed);
    EXPECT_TRUE(l.ping(seq(1),false,16000000));
    EXPECT_EQ(l.silence(30999999),Silence::None);
    EXPECT_EQ(l.silence(31000000),Silence::Lost);
    EXPECT_EQ(l.timeouts,2u);
}
TEST(Liveness, PingJustBeforeTheThresholdStartsANewInterval) {
    dg::DeviceLiveness l;l.ready(1,0);
    EXPECT_TRUE(l.ping(seq(1),false,14999999));
    EXPECT_EQ(l.silence(15000000),Silence::None);
    EXPECT_EQ(l.silence(29999998),Silence::None);
    EXPECT_EQ(l.silence(29999999),Silence::Lost);
}
TEST(Liveness, RejectedPingNeitherArmsNorRefreshes) {
    dg::DeviceLiveness l;l.ready(1,0);
    EXPECT_FALSE(l.ping("{\"t\":\"ping\",\"seq\":0}",false,14000000));
    EXPECT_EQ(l.silence(15000000),Silence::Unarmed);
    EXPECT_TRUE(l.ping(seq(5),false,16000000));
    EXPECT_FALSE(l.ping(seq(5),false,30000000));  // A replay does not refresh the interval.
    EXPECT_EQ(l.silence(31000000),Silence::Lost);
}
TEST(Liveness, NewGenerationStartsUnarmedWhateverTheLastOneDid) {
    dg::DeviceLiveness l;l.ready(1,0);
    EXPECT_TRUE(l.ping(seq(1),false,1000000));
    EXPECT_EQ(l.silence(16000000),Silence::Lost);
    l.ready(2,20000000);
    EXPECT_EQ(l.silence(34999999),Silence::None);
    EXPECT_EQ(l.silence(35000000),Silence::Unarmed);  // The old generation's pings do not arm this one.
    EXPECT_TRUE(l.ping(seq(1),false,36000000));  // Sequence numbers restart with the generation.
    EXPECT_EQ(l.silence(51000000),Silence::Lost);
    l.ready(3,60000000);
    EXPECT_EQ(l.silence(60000000),Silence::None);EXPECT_EQ(l.timeouts,0u);
}
