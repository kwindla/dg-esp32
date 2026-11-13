#pragma once
#include "pcm.h"
#include <algorithm>
#include <cstdint>
namespace dg_audio {
// Aperiodic, reproducible 10%-peak DAC excitation: 200ms silence, 2s noise,
// 200ms silence. No per-block phase reset or allocation; 10ms edge envelope.
class AecProbe {
public:
    static constexpr unsigned samples=38400;
    int16_t next(unsigned index) {
        if(index<3200 || index>=35200)return 0;
        state_^=state_<<13;state_^=state_>>17;state_^=state_<<5;
        const int32_t value=signed16(state_>>16)/10;
        const unsigned envelope=std::min({160u,index-3200,35200-index});
        return int16_t(value*int32_t(envelope)/160);
    }
private:
    uint32_t state_=0x13579bdf;
};
}
