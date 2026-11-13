#pragma once
#include <cstdint>
namespace dg_display {
// Diagnostic only: no task, buffers or heap allocations until explicitly armed.
void r3_stress_start(unsigned burst_us,unsigned period_us,unsigned phase_us,unsigned core=1);
void r3_stress_stop();
void r3_stress_status(void (*out)(const char*));
void r3_stress_dma_start() noexcept;
void r3_stress_dma_done(int64_t start,int64_t end) noexcept;
}
