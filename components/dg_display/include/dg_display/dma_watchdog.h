#pragma once
#include <cstdint>
namespace dg_display {
// Wall time spent inside an API is not proof of hardware loss: the owner may
// have been descheduled before queueing or after the completion ISR. Only a
// started color transfer with an absent completion can trip this watchdog.
inline bool missing_dma_completion(int64_t now,int64_t stripe,int64_t dma_start,
        int64_t completion,bool color_pending) {
    return color_pending && stripe>0 && dma_start>=stripe && completion<stripe && now-dma_start>2000000;
}
}
