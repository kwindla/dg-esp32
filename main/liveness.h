#pragma once
#include <atomic>
#include <cstdint>
#include <nlohmann/json.hpp>
namespace dg {
inline bool session_work_current(uint32_t work,uint32_t callback,uint32_t current,bool ready) {
    return ready && work==callback && work==current;
}
// Control owner sets deadlines; session worker takes one compact pong mailbox.
class DeviceLiveness {
public:
    enum class Ping {NotPing,Accepted,Rejected};
    std::atomic<unsigned> pings{0},pongs{0},timeouts{0};
    void ready(uint32_t generation,int64_t now) {
        gen_=generation;last_=now;warned_=false;pending_=0;sequence_=0;pings=0;pongs=0;timeouts=0;
    }
    Ping receive(const char* text,bool truncated,int64_t now) {
        if(truncated)return Ping::NotPing;
        auto d=nlohmann::json::parse(text,nullptr,false);
        if(!d.is_object() || !d.contains("t") || d["t"]!="ping")return Ping::NotPing;
        if(!d.contains("seq") || !d["seq"].is_number_unsigned())return Ping::Rejected;
        auto seq=d["seq"].get<uint64_t>();if(!seq || seq>UINT32_MAX || seq<=sequence_)return Ping::Rejected;
        sequence_=uint32_t(seq);
        ++pings;last_=now;warned_=false;pending_=(uint64_t(gen_)<<32)|seq;return Ping::Accepted;
    }
    bool ping(const char* text,bool truncated,int64_t now) {return receive(text,truncated,now)==Ping::Accepted;}
    uint64_t take(){return pending_.exchange(0);}
    // One report per silent interval; an accepted ping starts a new interval.
    bool missing(int64_t now) {
        if(!warned_ && now-last_>=15000000){warned_=true;++timeouts;return true;}
        return false;
    }
    // What fifteen seconds without a ping mean for a Ready session. A bot that
    // has pinged and fell silent (restart, lost NAT path, hung worker) has lost
    // the session: nothing else ends it. A bot that has never pinged in this
    // generation, as server/bot.py never does, is unarmed and keeps it.
    enum class Silence {None,Unarmed,Lost};
    Silence silence(int64_t now) {
        if(!missing(now))return Silence::None;
        return pings.load()?Silence::Lost:Silence::Unarmed;
    }
private:
    uint32_t gen_=0,sequence_=0;int64_t last_=0;bool warned_=false;
    std::atomic<uint64_t> pending_{0};
};
}
