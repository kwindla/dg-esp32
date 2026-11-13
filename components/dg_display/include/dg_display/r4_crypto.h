#pragma once
#include <cstddef>
#include <cstdint>
namespace dg_display {
void r4_asset_context(bool active) noexcept;
bool runtime_crypto_gate_ready() noexcept;
bool runtime_crypto_gate_take() noexcept;
int r4_crypto_gate_begin(const void* input,size_t input_bytes,const void* extra,size_t extra_bytes) noexcept;
void r4_crypto_gate_end(bool held) noexcept;
void r4_crypto_guard(unsigned mode);
struct R4CryptoHealth {
    bool lcd_tx_captured=false;
    int lcd_tx_id=-1,tx_pri=-1;
    unsigned priority_applies=0,last_priority_written=0,coverage_lost=0,gate_failures=0;
    uint64_t sha_psram_input=0,sha_internal_input=0,aes_psram_input=0,aes_internal_input=0;
    uint64_t psram_output_capacity=0,internal_output_capacity=0;
};
R4CryptoHealth r4_crypto_health() noexcept;
void r4_gdma_ready_once() noexcept;
void r4_crypto_status(unsigned start,void (*out)(const char*));
struct R4AssetScope {R4AssetScope(){r4_asset_context(true);}~R4AssetScope(){r4_asset_context(false);}};
}
