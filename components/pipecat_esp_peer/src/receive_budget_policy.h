#pragma once
#include <cstdint>
#include <sys/select.h>
namespace pipecat::esp_peer {
// Owner-only state. Idle select keeps its native timeout; the first successful
// socket read activates the original pass deadline. Setup is exempt until
// chat opens or authenticated, authorized media reaches the receiver.
// UDP/STUN uses recvfrom. TCP/TLS (TURN) uses recv(), which is not wrapped
// and never arms the after-read deadline; this budget applies to UDP only.
struct ReceiveBudget {
    int64_t deadline_us=0,pass_deadline_us=0;
    int64_t pass_begin_us=0;
    bool awaiting_read=false;
    bool media_active=false;
    unsigned packets=0,bytes=0;
    static constexpr unsigned packet_limit=4,byte_limit=4096;
    void begin(bool opened,bool after_read,int64_t now_us) {
        packets=bytes=0;
        pass_begin_us=now_us;
        const bool bounded=opened || media_active;
        awaiting_read=bounded && after_read;
        pass_deadline_us=bounded?now_us+4000:0;
        deadline_us=bounded && !after_read?pass_deadline_us:0;
    }
    // Called before processing valid receiver-transform media. The wrapper has
    // already counted its UDP read. Arm THIS native pass, retaining all reads,
    // bytes and elapsed time since begin; later media cannot renew the budget.
    // Persist across finish/begin while SCTP chat is still handshaking.
    bool admitted_media() {
        const bool first=!media_active;
        media_active=true;awaiting_read=false;
        if(!pass_deadline_us)pass_deadline_us=pass_begin_us+4000;
        deadline_us=(packets>=packet_limit || bytes>=byte_limit)?1:pass_deadline_us;
        return first;
    }
    void received(unsigned size=0) {
        ++packets;bytes+=size;
        if(awaiting_read) {deadline_us=pass_deadline_us;awaiting_read=false;}
        if(pass_deadline_us && (packets>=packet_limit || bytes>=byte_limit))deadline_us=1;
    }
    bool exhausted(int64_t now) const {return deadline_us && now>=deadline_us;}
    void finish() {deadline_us=0;pass_deadline_us=0;awaiting_read=false;}
};
// Zero means an ordinary select timeout with cleared read/error sets.
// Minus one means continue to native select, preserving any native error.
// A non-null write set preserves the native operation unconditionally.
inline int bound_receive_select(int64_t deadline_us,int64_t now_us,
        fd_set* read,fd_set* write,fd_set* error,timeval*& timeout,timeval& bounded) {
    if(!deadline_us || !read || write)return -1;
    const auto remaining=deadline_us-now_us;
    if(remaining<=0) {FD_ZERO(read);if(error)FD_ZERO(error);return 0;}
    if(!timeout || timeout->tv_sec*1000000LL+timeout->tv_usec>remaining) {
        bounded.tv_sec=remaining/1000000;bounded.tv_usec=remaining%1000000;
        timeout=&bounded;
    }
    return -1;
}
}
