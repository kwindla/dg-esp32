#pragma once
#ifndef DG_APP_HOST
#include "sdkconfig.h"
#endif
#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
namespace dg {
struct MediaToken;
enum class State { Boot, WifiConnecting, Idle, Starting, Connected, Ready, Disconnected };
enum class EventKind { Boot, Connect, Disconnect, Reconnect, WifiUp, WifiDown, IpChanged, WifiFailed,
    Connected, Ready, Failure, Stopped, Tick, UserStart, UserStop, UserText, BotText,
    BotStart, BotStop, BotInterrupt, Server, Metrics
#if CONFIG_DG_DUO
    ,DuoCommand,DuoSendFailed
#endif
};
struct Event { EventKind kind=EventKind::Tick; uint32_t generation=0; char text[1024]{}; bool truncated=false; int64_t network_at_us=0; };
enum class ActionKind { JoinWifi, Start, Stop };
struct Action { ActionKind kind; uint32_t generation; };
struct Actions { std::array<Action,3> items{}; size_t count=0;
    void add(ActionKind kind,uint32_t generation) { items[count++]={kind,generation}; } };
const char* state_name(State state);
enum class DemoPhase { Connecting, Live, Disconnected };
// Called only for accepted policy transitions, including actual bot-ready.
std::optional<DemoPhase> demo_transition(State previous,State current);
// Pure, single-owner policy. All time values are monotonic milliseconds.
class StateMachine {
public:
    Actions dispatch(const Event& event,int64_t now);
    State state() const {return state_;}
    uint32_t generation() const {return generation_;}
    bool desired_connected() const {return desired_;}
    unsigned backoff_ms() const {return backoff_;}
    int64_t retry_at() const {return retry_at_;}
    bool accepts(uint32_t generation) const {return session_ && generation==generation_;}
private:
    State state_=State::Boot;
    bool desired_=false,wifi_=false,joining_=false,session_=false,stopping_=false;
    uint32_t generation_=0;
    unsigned backoff_=1000,wifi_backoff_=1000;
    int64_t retry_at_=0,wifi_retry_at_=0,ready_at_=0,wifi_up_at_us_=0;
    void stop(Actions& actions);
    void advance(Actions& actions,int64_t now);
};
#ifndef DG_APP_HOST
}
#include "dg_audio/capability.h"
namespace dg {
namespace detail {struct AppImpl;}
}
namespace pipecat {class PipecatClient;}
namespace nlohmann { }
namespace dg {
// Blocking network/client work lives on a worker; callbacks only enqueue.
class App {
public:
    struct Hooks {std::function<void()> before_connect,ready,stop; std::function<void(bool)> speaking; std::function<void()> join_producers;
        // Control-owner callback: bounded mailbox admission only, never IO/waits.
        std::function<void(DemoPhase,uint32_t)> lifecycle{};
        std::function<dg_audio::Capability()> audio_capability{};};
    explicit App(Hooks hooks,std::string url={});
    ~App();
    void connect();
    void request_connect(); // queue intent without waiting for WiFi/bot
    bool request_reconnect(); // zero-wait event admission; ignored unless Live

    void server_url(const std::string& url);
#if CONFIG_DG_DUO
    bool duo_command(unsigned command,unsigned argument=0); // 0=status, 1=enable, 2=tap
#endif
    void disconnect();
    void wait_wifi();
    void fault_next_channel();
    int send_audio(const int16_t* pcm,size_t n);
    bool audio_ready();
    int read_audio(int16_t* pcm,size_t n);
    int read_audio(int16_t* pcm,size_t n,MediaToken& token);
    void with_client(const std::function<void(pipecat::PipecatClient&)>& fn);
    std::string stats();
    void dump_uplink_intervals();
    size_t drain_trace(void* records,size_t capacity);
    void diagnostic_option(const std::string& name,unsigned value);
    bool ready() const;
    bool idle() const;
private:
    std::unique_ptr<detail::AppImpl> impl_;
};
#endif
}
