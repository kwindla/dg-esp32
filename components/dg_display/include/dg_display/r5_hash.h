#pragma once
#include <cstdint>
namespace dg_display {
// Diagnostic only; no task or buffers until explicitly armed.
void r5_hash_start(unsigned core,unsigned mode);
void r5_hash_stop();
void r5_hash_status(void (*out)(const char*));
void r5_hash_dma_start() noexcept;
void r5_hash_dma_done(int64_t start,int64_t end) noexcept;
void r5_hash_error(unsigned index) noexcept;
bool runtime_hash_gate_take() noexcept;
void runtime_hash_gate_give() noexcept;
}
