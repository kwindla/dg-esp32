#include "app.h"
#include <gtest/gtest.h>
TEST(NetworkOrder, WifiUpRejectsFlagsThatPrecededIt) {
    dg::StateMachine m;m.dispatch({dg::EventKind::Boot},0);
    dg::Event up{dg::EventKind::WifiUp};up.network_at_us=1000;m.dispatch(up,1);
    m.dispatch({dg::EventKind::Connect},2);auto generation=m.generation();
    m.dispatch({dg::EventKind::Ready,generation},3);
    for(auto kind:{dg::EventKind::WifiDown,dg::EventKind::IpChanged}) {
        dg::Event flag{kind};flag.network_at_us=999;
        EXPECT_EQ(m.dispatch(flag,4).count,0u);EXPECT_EQ(m.state(),dg::State::Ready);
    }
    dg::Event fresh{dg::EventKind::IpChanged};fresh.network_at_us=1001;
    EXPECT_EQ(m.dispatch(fresh,5).count,1u);EXPECT_EQ(m.state(),dg::State::WifiConnecting);
}
TEST(NetworkOrder, FableIpChangingRejoinSequenceDoesNotRestartTwice) {
    dg::StateMachine m;m.dispatch({dg::EventKind::Boot},0);m.dispatch({dg::EventKind::WifiUp},1);
    m.dispatch({dg::EventKind::Connect},2);m.dispatch({dg::EventKind::Ready,m.generation()},3);
    m.dispatch({dg::EventKind::WifiDown},10);m.dispatch({dg::EventKind::Stopped,m.generation()},11);
    dg::Event up{dg::EventKind::WifiUp};up.network_at_us=21000;
    auto actions=m.dispatch(up,21);ASSERT_EQ(actions.count,1u);EXPECT_EQ(actions.items[0].kind,dg::ActionKind::Start);
    auto generation=m.generation();
    dg::Event changed{dg::EventKind::IpChanged};changed.network_at_us=20000;
    EXPECT_EQ(m.dispatch(changed,21).count,0u);EXPECT_EQ(m.generation(),generation);
    EXPECT_EQ(m.state(),dg::State::Starting);
}
using namespace dg;
struct AppTest:testing::Test {
    StateMachine m;
    uint32_t begin() {
        EXPECT_EQ(m.dispatch({EventKind::Boot},0).items[0].kind,ActionKind::JoinWifi);
        m.dispatch({EventKind::WifiUp},100);
        auto a=m.dispatch({EventKind::Connect},200);EXPECT_EQ(a.count,1u);EXPECT_EQ(a.items[0].kind,ActionKind::Start);
        return m.generation();
    }
    uint32_t ready() {auto g=begin();m.dispatch({EventKind::Connected,g},300);m.dispatch({EventKind::Ready,g},400);return g;}
};
TEST_F(AppTest, BootJoinsWifiAndWaitsForIntent) {m.dispatch({EventKind::Boot},0);auto a=m.dispatch({EventKind::WifiUp},1);EXPECT_EQ(m.state(),State::Idle);EXPECT_FALSE(m.desired_connected());EXPECT_EQ(a.count,0u);}
TEST_F(AppTest, OrderedConnectionStates) {auto g=begin();EXPECT_EQ(m.state(),State::Starting);m.dispatch({EventKind::Connected,g},300);EXPECT_EQ(m.state(),State::Connected);m.dispatch({EventKind::Ready,g},400);EXPECT_EQ(m.state(),State::Ready);}
TEST_F(AppTest, ReadyMayPrecedeConnectedWithoutRegressing) {auto g=begin();m.dispatch({EventKind::Ready,g},300);m.dispatch({EventKind::Connected,g},400);EXPECT_EQ(m.state(),State::Ready);}
TEST_F(AppTest, UserDisconnectClearsIntentBeforeTeardown) {auto g=ready();auto a=m.dispatch({EventKind::Disconnect},500);EXPECT_FALSE(m.desired_connected());EXPECT_EQ(a.count,1u);EXPECT_EQ(a.items[0].kind,ActionKind::Stop);EXPECT_FALSE(m.accepts(g));m.dispatch({EventKind::Failure,g},501);m.dispatch({EventKind::Stopped,m.generation()},502);EXPECT_EQ(m.dispatch({EventKind::Tick},100000).count,0u);EXPECT_EQ(m.state(),State::Idle);}
TEST_F(AppTest, FatalAndDisconnectedPairStopsOnce) {auto g=ready();auto a=m.dispatch({EventKind::Failure,g},500);EXPECT_EQ(a.count,1u);EXPECT_EQ(m.state(),State::Disconnected);EXPECT_EQ(m.dispatch({EventKind::Failure,g},501).count,0u);EXPECT_EQ(m.retry_at(),1500);EXPECT_EQ(m.backoff_ms(),2000u);}
TEST_F(AppTest, DeadlineFailureTearsDownBeforeRetry) {auto g=begin();m.dispatch({EventKind::Failure,g},20000);EXPECT_EQ(m.dispatch({EventKind::Tick},40000).count,0u);auto a=m.dispatch({EventKind::Stopped,m.generation()},40001);ASSERT_EQ(a.count,1u);EXPECT_EQ(a.items[0].kind,ActionKind::Start);EXPECT_GT(m.generation(),g);}
TEST_F(AppTest, BackoffDoublesAndCapsAtThirtySeconds) {auto g=begin();int64_t now=500;for(unsigned expected: {1000u,2000u,4000u,8000u,16000u,30000u,30000u}) {m.dispatch({EventKind::Failure,g},now);EXPECT_EQ(m.retry_at(),now+expected);auto next=m.generation();m.dispatch({EventKind::Stopped,next},now+1);EXPECT_EQ(m.dispatch({EventKind::Tick},now+expected-1).count,0u);auto a=m.dispatch({EventKind::Tick},now+expected);ASSERT_EQ(a.count,1u);g=m.generation();now+=expected+1;}}
TEST_F(AppTest, BackoffDoesNotResetForShortSession) {auto g=ready();m.dispatch({EventKind::Failure,g},1000);auto stop=m.generation();m.dispatch({EventKind::Stopped,stop},1001);m.dispatch({EventKind::Tick},2000);g=m.generation();m.dispatch({EventKind::Ready,g},2100);m.dispatch({EventKind::Failure,g},62099);EXPECT_EQ(m.retry_at(),64099);}
TEST_F(AppTest, BackoffResetsAfterSixtyStableSeconds) {auto g=ready();m.dispatch({EventKind::Failure,g},1000);m.dispatch({EventKind::Stopped,m.generation()},1001);m.dispatch({EventKind::Tick},2000);g=m.generation();m.dispatch({EventKind::Ready,g},2100);m.dispatch({EventKind::Failure,g},62100);EXPECT_EQ(m.retry_at(),63100);}
TEST_F(AppTest, StaleCallbacksCannotMutateNewSession) {auto old=ready();m.dispatch({EventKind::Failure,old},500);m.dispatch({EventKind::Stopped,m.generation()},501);m.dispatch({EventKind::Tick},1500);auto g=m.generation();m.dispatch({EventKind::Connected,old},1501);m.dispatch({EventKind::Ready,old},1502);m.dispatch({EventKind::Failure,old},1503);EXPECT_EQ(m.state(),State::Starting);EXPECT_EQ(m.generation(),g);}
TEST_F(AppTest, WifiLossStopsThenRejoinsThenReconnects) {auto g=ready();auto a=m.dispatch({EventKind::WifiDown},500);ASSERT_EQ(a.count,1u);EXPECT_EQ(a.items[0].kind,ActionKind::Stop);EXPECT_EQ(m.state(),State::WifiConnecting);EXPECT_TRUE(m.desired_connected());a=m.dispatch({EventKind::Stopped,m.generation()},501);ASSERT_EQ(a.count,1u);EXPECT_EQ(a.items[0].kind,ActionKind::JoinWifi);a=m.dispatch({EventKind::WifiUp},600);ASSERT_EQ(a.count,1u);EXPECT_EQ(a.items[0].kind,ActionKind::Start);EXPECT_GT(m.generation(),g);}
TEST_F(AppTest, IpChangeUsesSameRecoveryPolicy) {ready();m.dispatch({EventKind::IpChanged},500);EXPECT_EQ(m.state(),State::WifiConnecting);EXPECT_TRUE(m.desired_connected());}
TEST_F(AppTest, WifiRetriesWithoutSessionOrReboot) {m.dispatch({EventKind::Boot},0);m.dispatch({EventKind::WifiFailed},20000);EXPECT_EQ(m.dispatch({EventKind::Tick},20999).count,0u);auto a=m.dispatch({EventKind::Tick},21000);ASSERT_EQ(a.count,1u);EXPECT_EQ(a.items[0].kind,ActionKind::JoinWifi);EXPECT_EQ(m.generation(),0u);}
TEST_F(AppTest, UserDisconnectWhileStartingDropsLateReady) {auto g=begin();m.dispatch({EventKind::Disconnect},300);m.dispatch({EventKind::Ready,g},400);EXPECT_EQ(m.state(),State::Idle);m.dispatch({EventKind::Stopped,m.generation()},500);EXPECT_EQ(m.dispatch({EventKind::Tick},2000).count,0u);}
TEST_F(AppTest, ConnectWhileStoppingWaitsForTeardown) {ready();m.dispatch({EventKind::Disconnect},500);EXPECT_EQ(m.dispatch({EventKind::Connect},501).count,0u);auto a=m.dispatch({EventKind::Stopped,m.generation()},502);ASSERT_EQ(a.count,1u);EXPECT_EQ(a.items[0].kind,ActionKind::Start);}
TEST_F(AppTest, FailureDuringStoppingAllowsLaterStart) {
    auto g=ready();m.dispatch({EventKind::Failure,g},500);
    m.dispatch({EventKind::Failure,m.generation()},501);
    auto a=m.dispatch({EventKind::Tick},1500);
    ASSERT_EQ(a.count,1u);EXPECT_EQ(a.items[0].kind,ActionKind::Start);
}
TEST_F(AppTest, TenThousandTickWedgeThenManualReconnectRecovers) {
    auto g=ready();m.dispatch({EventKind::Failure,g},500);
    m.dispatch({EventKind::Failure,m.generation()},501);
    unsigned starts=0;
    for(int i=0;i<10000;++i) {auto a=m.dispatch({EventKind::Tick},502+i);for(size_t j=0;j<a.count;++j)starts+=a.items[j].kind==ActionKind::Start;}
    EXPECT_EQ(starts,1u);
    m.dispatch({EventKind::Disconnect},11000);
    m.dispatch({EventKind::Stopped,m.generation()},11001);
    auto a=m.dispatch({EventKind::Connect},11002);
    ASSERT_EQ(a.count,1u);EXPECT_EQ(a.items[0].kind,ActionKind::Start);
}
TEST_F(AppTest, LostWifiUpFailureReissuesJoin) {
    m.dispatch({EventKind::Boot},0);m.dispatch({EventKind::Failure},20000);
    auto a=m.dispatch({EventKind::Tick},21000);
    ASSERT_EQ(a.count,1u);EXPECT_EQ(a.items[0].kind,ActionKind::JoinWifi);
}
TEST_F(AppTest, ManualConnectResetsBackoff) {
    auto g=ready();m.dispatch({EventKind::Failure,g},500);
    m.dispatch({EventKind::Stopped,m.generation()},501);
    m.dispatch({EventKind::Tick},1500);g=m.generation();
    m.dispatch({EventKind::Failure,g},1501);
    m.dispatch({EventKind::Stopped,m.generation()},1502);
    m.dispatch({EventKind::Disconnect},1503);m.dispatch({EventKind::Connect},1504);
    m.dispatch({EventKind::Failure,m.generation()},1505);
    EXPECT_EQ(m.retry_at(),2505);EXPECT_EQ(m.backoff_ms(),2000u);
}

#include "app_events.h"
#include "uplink_intervals.h"
TEST(AppEvents, MalformedAndOversizedTranscriptsAreIgnored) {
    TurnTranscript out{42,"unchanged"};
    for(auto text:{"{}","{", "[]", "{\"t\":7}","{\"t\":\"turn-transcript\"}",
                  "{\"t\":\"turn-transcript\",\"d\":[]}",
                  "{\"t\":\"turn-transcript\",\"d\":{\"turn\":-1,\"text\":\"a\"}}",
                  "{\"t\":\"turn-transcript\",\"d\":{\"turn\":1,\"text\":2}}",
                  "{\"t\":\"turn-transcript\",\"d\":{\"turn\":4294967296,\"text\":\"a\"}}"}) {
        EXPECT_FALSE(turn_transcript(text_event(EventKind::Server,1,text),out));
        EXPECT_EQ(out.turn,42u);
    }
    auto oversized=nlohmann::json{{"t","turn-transcript"},{"d",{{"turn",3},{"text",std::string(2000,'x')}}}}.dump();
    auto large=text_event(EventKind::Server,1,oversized.c_str());
    EXPECT_TRUE(large.truncated);EXPECT_FALSE(turn_transcript(large,out));
    auto valid=text_event(EventKind::Server,1,"{\"t\":\"turn-transcript\",\"d\":{\"turn\":3,\"text\":\"France\"}}");
    EXPECT_FALSE(valid.truncated);ASSERT_TRUE(turn_transcript(valid,out));EXPECT_EQ(out.turn,3u);EXPECT_EQ(out.text,"France");
}
TEST(AppEvents, EnqueueDuringCompletionCannotClearBusy) {
    PendingActions work;work.add({ActionKind::Start,1});
    work.flush([](Action){return true;});
    work.add({ActionKind::Stop,2}); // control queues B before worker completes A
    work.completed();EXPECT_TRUE(work.busy());
    work.flush([](Action){return true;});work.completed();EXPECT_FALSE(work.busy());
}
TEST(AppEvents, FullWorkQueueRetainsWifiJoinAndRejoins) {
    StateMachine model;PendingActions work;
    auto a=model.dispatch({EventKind::Boot},0);work.add(a.items[0]);
    work.flush([](Action){return false;});EXPECT_TRUE(work.busy());
    unsigned delivered=0;
    work.flush([&](Action action){EXPECT_EQ(action.kind,ActionKind::JoinWifi);++delivered;return true;});
    EXPECT_EQ(delivered,1u);model.dispatch({EventKind::WifiUp},100);work.completed();
    EXPECT_EQ(model.state(),State::Idle);EXPECT_FALSE(work.busy());
    a=model.dispatch({EventKind::WifiDown},200);ASSERT_EQ(a.count,1u);EXPECT_EQ(a.items[0].kind,ActionKind::JoinWifi);
}
TEST(AppEvents, OverflowDoesNotOverwriteStoppedOrUserDisconnect) {
    StateMachine model;model.dispatch({EventKind::Boot},0);model.dispatch({EventKind::WifiUp},1);
    model.dispatch({EventKind::Connect},2);auto old=model.generation();
    model.dispatch({EventKind::Failure,old},3);
    event_and_overflow({EventKind::Stopped,model.generation()},old,[&](const Event& e){model.dispatch(e,4);});
    ASSERT_EQ(model.dispatch({EventKind::Tick},1003).count,1u);
    event_and_overflow({EventKind::Disconnect},old,[&](const Event& e){model.dispatch(e,1004);});
    EXPECT_FALSE(model.desired_connected());
}
TEST(AppEvents, GenerationZeroOverflowCannotFailCurrentSession) {
    EXPECT_EQ(overflow_session({EventKind::Disconnect}),0u);
    EXPECT_EQ(overflow_session({EventKind::WifiDown}),0u);
    EXPECT_EQ(overflow_session({EventKind::Metrics,17}),0u);
    EXPECT_EQ(overflow_session({EventKind::Reconnect,17}),0u);
    EXPECT_EQ(overflow_session({EventKind::Server,17}),0u);
    EXPECT_EQ(overflow_session({EventKind::Failure,17}),17u);
}
TEST(AppEvents, StopScopeGuardCompletesAfterErrorIsLogged) {
    std::string order;
    {ScopeExit completed([&]{order+="Stopped";});
     try {throw std::runtime_error("teardown");}catch(...) {order+="error;";}}
    EXPECT_EQ(order,"error;Stopped");
}

TEST_F(AppTest, ReconnectShowsDisconnectedAndWaitsForTeardownAndDwell) {
    auto old=ready();auto a=m.dispatch({EventKind::Reconnect,m.generation()},500);
    ASSERT_EQ(a.count,1u);EXPECT_EQ(a.items[0].kind,ActionKind::Stop);
    EXPECT_EQ(m.state(),State::Disconnected);EXPECT_TRUE(m.desired_connected());
    EXPECT_FALSE(m.accepts(old));
    EXPECT_EQ(m.dispatch({EventKind::Reconnect,m.generation()},501).count,0u);
    EXPECT_EQ(m.dispatch({EventKind::Stopped,m.generation()},502).count,0u);
    EXPECT_EQ(m.dispatch({EventKind::Tick},799).count,0u);
    a=m.dispatch({EventKind::Tick},800);ASSERT_EQ(a.count,1u);
    EXPECT_EQ(a.items[0].kind,ActionKind::Start);EXPECT_GT(m.generation(),old);
    EXPECT_EQ(m.dispatch({EventKind::Reconnect,m.generation()},801).count,0u);
    m.dispatch({EventKind::Ready,old},802);EXPECT_EQ(m.state(),State::Starting);
}
TEST_F(AppTest, ThirtyIdleTimeoutsKeepAutostartIntentAndAcceptOnlyCurrentReady) {
    ready();int64_t now=1000;
    for(unsigned i=0;i<30;++i) {
        auto old=m.generation();now+=300000;
        EXPECT_EQ(m.dispatch({EventKind::Failure,old},now).count,1u);
        EXPECT_TRUE(m.desired_connected());
        auto stopped=m.dispatch({EventKind::Stopped,m.generation()},now+1);
        EXPECT_EQ(stopped.count,0u);
        auto a=m.dispatch({EventKind::Tick},now+1000);ASSERT_EQ(a.count,1u);
        m.dispatch({EventKind::Ready,old},now+1001);EXPECT_EQ(m.state(),State::Starting);
        m.dispatch({EventKind::Ready,m.generation()},now+1002);EXPECT_EQ(m.state(),State::Ready);
    }
}
TEST(DemoLifecycle, EmitsOnlyRealTransitions) {
    EXPECT_EQ(demo_transition(State::Idle,State::Starting),DemoPhase::Connecting);
    EXPECT_FALSE(demo_transition(State::Starting,State::Connected));
    EXPECT_EQ(demo_transition(State::Connected,State::Ready),DemoPhase::Live);
    EXPECT_FALSE(demo_transition(State::Starting,State::Connected));
    EXPECT_FALSE(demo_transition(State::Ready,State::Ready));
    EXPECT_EQ(demo_transition(State::Ready,State::WifiConnecting),DemoPhase::Disconnected);
    EXPECT_EQ(demo_transition(State::Ready,State::Disconnected),DemoPhase::Disconnected);
    EXPECT_FALSE(demo_transition(State::Disconnected,State::Idle));
}

TEST_F(AppTest, QueuedReadyCannotAdmitTapFromReconnectOrAnOldGeneration) {
    auto old=ready();m.dispatch({EventKind::Reconnect,old},500);
    m.dispatch({EventKind::Stopped,m.generation()},501);m.dispatch({EventKind::Tick},800);
    ASSERT_EQ(m.state(),State::Starting);
    auto during=reconnect_event(m.state(),m.generation());EXPECT_FALSE(during);
    m.dispatch({EventKind::Ready,m.generation()},801);
    EXPECT_EQ(m.dispatch({EventKind::Reconnect,old},802).count,0u);
    EXPECT_EQ(m.state(),State::Ready);
    auto current=reconnect_event(m.state(),m.generation());ASSERT_TRUE(current);
    EXPECT_EQ(m.dispatch(*current,803).items[0].kind,ActionKind::Stop);
}

#include "demo_hold.h"
TEST(DemoHold, TeardownPrecedesConfigAndRestoreAndFailureKeepsSessionGuard) {
    bool peer=true,display_session=true;
    auto configure_and_restore=[&]{return !peer && !display_session;};
    EXPECT_FALSE(configure_and_restore());
    EXPECT_THROW(finish_demo_hold([]{throw std::runtime_error("unfinished teardown");},[&]{display_session=false;}),std::runtime_error);
    EXPECT_TRUE(display_session);
    finish_demo_hold([&]{peer=false;},[&]{EXPECT_FALSE(peer);display_session=false;});
    EXPECT_TRUE(configure_and_restore());
}

#include "demo_intent.h"
TEST(DemoIntent, FastHoldRunAndManualDisconnectRunNeedNoWorkerPollToRearm) {
    DemoIntent intent;intent.activated=true;
    intent.hold();EXPECT_TRUE(intent.held);EXPECT_FALSE(intent.activated);
    intent.resume();EXPECT_FALSE(intent.held);EXPECT_FALSE(intent.activated);
    intent.activated=true; // A console disconnect can leave the prior activation marked.
    intent.resume();EXPECT_FALSE(intent.activated);
}

#include "start_request.h"
TEST(AudioCapability, StartEnvelopeNestsBodyAndPreservesBoolean) {
    for(bool enabled:{false,true}) {
        auto request=dg::start_request({enabled,15,400});
        EXPECT_EQ(request["transport"],"webrtc");EXPECT_FALSE(request.contains("audio"));
        const auto& audio=request["body"]["audio"];
        EXPECT_EQ(audio["v"],1);EXPECT_TRUE(audio["aec"].is_boolean());
        EXPECT_EQ(audio["aec"],enabled);EXPECT_EQ(audio["ref_gain_db"],15);EXPECT_EQ(audio["guard_ms"],400);
    }
    EXPECT_EQ(dg::start_request({})["body"]["audio"]["aec"],false);
}

// Exercise the same stop adapter used by AppImpl with the real interval state.
TEST(AppEvents, StopFreezesIntervalsBeforeCaptureDisableAndTeardownWait) {
    struct Peer {
        pipecat::esp_peer::UplinkIntervals intervals{1};
        int64_t now=1000;
        void freeze_uplink() {intervals.freeze(now);}
    } peer;
    peer.intervals.submit(peer.now,true);
    for(peer.now+=20000;peer.now<=14001000;peer.now+=20000)peer.intervals.submit(peer.now,true);
    const auto stopped_at=peer.now;
    bool capture=true;
    dg::stop_capture(peer,[&]{capture=false;peer.now+=2000000;});
    EXPECT_FALSE(capture);
    // Disconnect's later fallback cannot extend the frozen window.
    peer.freeze_uplink();
    pipecat::esp_peer::UplinkSnapshot frozen;ASSERT_TRUE(peer.intervals.claim(peer.now,frozen));
    EXPECT_EQ(frozen.end_us,stopped_at);EXPECT_EQ(frozen.last_ok_age_us,20000);
    EXPECT_EQ(frozen.n,600u);EXPECT_EQ(frozen.ok,601u);
    EXPECT_GE(frozen.ok,0.95*(frozen.end_us-frozen.start_us)/20000);
    EXPECT_LE(frozen.last_ok_age_us,60000);
}
