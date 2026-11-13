#pragma once
#include "app.h"
#include <nlohmann/json.hpp>
#include <atomic>
#include <cstdio>
#include <deque>
#include <limits>

namespace dg {
inline Event text_event(EventKind kind,uint32_t generation,const char* text) {
    Event e{kind,generation};
    const int length=std::snprintf(e.text,sizeof(e.text),"%s",text);
    e.truncated=length<0 || size_t(length)>=sizeof(e.text);
    return e;
}
inline std::optional<Event> reconnect_event(State published,uint32_t generation) {
    if(published!=State::Ready)return {};
    return Event{EventKind::Reconnect,generation};
}
struct TurnTranscript {unsigned turn;std::string text;};
inline bool turn_transcript(const Event& e,TurnTranscript& out) {
    if(e.truncated)return false;
    try {
        const auto m=nlohmann::json::parse(e.text);
        if(!m.is_object() || !m.contains("t") || !m["t"].is_string() || m["t"]!="turn-transcript" ||
           !m.contains("d") || !m["d"].is_object())return false;
        const auto& d=m["d"];
        if(!d.contains("turn") || !d["turn"].is_number_unsigned() ||
           d["turn"].get<uint64_t>()>std::numeric_limits<unsigned>::max() ||
           !d.contains("text") || !d["text"].is_string())return false;
        out={d["turn"].get<unsigned>(),d["text"].get<std::string>()};return true;
    }catch(...) {return false;}
}
// The control thread retains actions until the worker queue accepts them.
// It never blocks on the work queue: workers may be posting a completion to
// the event queue. pending includes both retained and worker-owned actions.
class PendingActions {
    std::deque<Action> retained_;
    std::atomic<unsigned> pending_{0};
public:
    void add(Action a) {retained_.push_back(a);++pending_;}
    template<class Send> void flush(Send send) {
        while(!retained_.empty() && send(retained_.front()))retained_.pop_front();
    }
    void completed() {--pending_;}
    bool busy() const {return pending_.load()!=0;}
};
template<class F> class ScopeExit {
    F fn_;
public:
    explicit ScopeExit(F f):fn_(std::move(f)){}
    ~ScopeExit() {fn_();}
    ScopeExit(const ScopeExit&)=delete;
};
template<class F> ScopeExit(F)->ScopeExit<F>;
// Dispatch the real event first. Overflow is additional evidence, never a
// replacement for a dequeued lifecycle completion or user command.
template<class Handle> void event_and_overflow(const Event& e,uint32_t overflow,Handle handle) {
    handle(e);
    if(overflow)handle(text_event(EventKind::Failure,overflow,"app event queue overflow"));
}
// Freeze the measured generation before the hook disables capture. Teardown
// may wait for producers, borrows and the SDK; none of that belongs in the window.
template<class Transport,class Stop> void stop_capture(Transport& transport,Stop stop) {
    transport.freeze_uplink();stop();
}
inline bool droppable_event(const Event& e) {return e.kind==EventKind::Server || e.kind==EventKind::Metrics;}
inline uint32_t overflow_session(const Event& e) {return droppable_event(e) || e.kind==EventKind::Reconnect ? 0 : e.generation;}
}
