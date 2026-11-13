#pragma once
#include <algorithm>
#include <cstdint>
namespace dg {
// When a touch owner probes its controller. The first probe is immediate. A
// controller that does not answer (an I2C timeout at boot, say) is probed
// again after 1 s, then 2 s, doubling up to 30 s, until one probe succeeds.
class TouchInitRetry {
public:
    bool ready()const noexcept{return ready_;}
    bool due(int64_t now_ms)const noexcept{return !ready_ && now_ms>=due_ms_;}
    // Records a failed probe and returns the wait before the next one.
    unsigned failed(int64_t now_ms)noexcept{
        const unsigned wait=backoff_ms_;due_ms_=now_ms+wait;backoff_ms_=std::min(30000u,backoff_ms_*2);return wait;
    }
    void succeeded()noexcept{ready_=true;}
private:
    int64_t due_ms_=0;unsigned backoff_ms_=1000;bool ready_=false;
};
}
