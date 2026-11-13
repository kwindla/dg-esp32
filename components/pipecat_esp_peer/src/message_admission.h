#pragma once
#include <cstddef>
#include <cstdint>
#include <algorithm>
#include <string_view>
namespace pipecat::esp_peer {
// Bounds work made READY for dispatch/parsing: four KiB per pass, 128 KiB/s sustained.
// The callback copies every valid message into the owned inbound queue first, so
// copying is bounded by queue capacity (64 slots x wire limit), not by this budget.
// Owner-only, before classification. No allocation and no catch-up burst. Work
// beyond a pass's budget is not lost: it is held in the owned inbound queue and
// re-offered here first in later passes (InboundQueue::promote).
struct MessageWorkBudget {
    static constexpr uint64_t rate=128*1024,burst=4096;
    static constexpr unsigned message_limit=16; // Real RTVI reply/interruption bursts.
    uint64_t credit=burst*1000000,small_credit=1024*1000000ULL,last=0;unsigned messages=0,bytes=0;
    void begin(){messages=bytes=0;}
    bool admit(size_t size,uint64_t now) {
        if(last) {
            credit=std::min(burst*1000000,credit+(now-last)*rate);
            small_credit=std::min(uint64_t(1024)*1000000,small_credit+(now-last)*4096);
        }
        last=now;
        if(messages>=message_limit || size>burst-bytes)return false;
        const auto cost=size*1000000;
        if(cost<=credit)credit-=cost;
        // Reserve a bounded lane for compact control/ack/show records without
        // parsing an untrusted type on the native owner. Size is not privilege:
        // arbitrary small messages share this same 4 KiB/s, 1 KiB burst quota.
        else if(size<=512 && cost<=small_credit)small_credit-=cost;
        else return false;
        ++messages;bytes+=size;return true;
    }
};
// Limits apply BEFORE a JSON lexer, DOM, SDK copy, or callback capture.
struct MessageAdmission {
    static constexpr size_t wire_limit=2048, node_limit=80; // Nominal combined RTVI metrics need 66.
    // An inbound queue slot holds one admitted frame: the wire limit, or the
    // transport's own maximum when that is smaller. Never the 16 KiB outbound maximum.
    static constexpr size_t slot_bytes(size_t max_message_bytes) {
        return max_message_bytes<wire_limit?max_message_bytes:wire_limit;
    }
    static constexpr size_t internal_floor=81920, largest_floor=32768;
    // A non-allocating upper bound on JSON values/keys/containers. Escaped
    // punctuation inside strings is data, not structure. Syntax is checked later.
    static size_t nodes(std::string_view text) {
        size_t n=1;bool quoted=false,escaped=false;
        for(char c:text) {
            if(quoted) {if(escaped)escaped=false;else if(c=='\\')escaped=true;else if(c=='"')quoted=false;}
            else if(c=='"'){quoted=true;++n;}
            else if(c=='{' || c=='[' || c==',' || c==':')++n;
            if(n>node_limit)return n;
        }
        return n;
    }
    // Conservative live envelope for parser + rtvi::Message + one captured
    // callback and its dump, including map/string/allocator overhead. Only one
    // message is dispatched while the SDK callback loop is idle.
    static size_t reserve(size_t bytes,size_t nodes) {return 2048+8*bytes+192*nodes;}
    static bool memory(size_t free,size_t largest,size_t bytes,size_t nodes) {
        auto needed=reserve(bytes,nodes);
        return free>=internal_floor+needed && largest>=largest_floor+bytes*2+1024;
    }
};
}
