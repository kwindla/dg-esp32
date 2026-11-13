#pragma once
#include "touch_init_retry.h"
#include <atomic>
#include <cstdint>
#include <thread>
namespace dg {
// Hardware owner only posts fixed tap events; it never reads the UI/protocol.
class DuoTouch {
public:
    using Send=bool(*)(void*,bool injected,uint32_t generation);
    DuoTouch(Send send,void* context):send_(send),context_(context){}
    ~DuoTouch(){stop();}
    void start();
    void stop();
    void admission(bool enabled_live,uint32_t generation); // app-control only
    void poll(int64_t now); // touch owner; deterministic entry for native checks
private:
    Send send_;void* context_;
    std::thread worker_;std::atomic<bool> quit_{false};
    std::atomic<uint64_t> window_{0};uint32_t serial_=0;
    TouchInitRetry init_;
    int64_t next_poll_=0;
};
}
