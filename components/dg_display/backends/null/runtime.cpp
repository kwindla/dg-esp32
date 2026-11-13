#include "dg_display.h"
#include "commands.h"
#include "dg_display/backend_commands.h"
#include "dg_display/runtime.h"
#include "dg_display/panel_config.h"
#include "dg_display/r3_stress.h"
#include "dg_display/r5_hash.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdexcept>
namespace dg_display {
namespace {bool attempted=false;}
void backend_boot(){census_init(true,StagingBytes);}
void census_init(bool external,size_t bytes){
    if(attempted)throw std::runtime_error("census init already attempted; reboot before retry");
    if(!external || bytes!=StagingBytes)throw std::runtime_error("null staging requires 20480 PSRAM bytes");
    attempted=true;
    auto* staging=static_cast<uint8_t*>(heap_caps_aligned_alloc(StagingAlignment,bytes,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    if(!staging)throw std::bad_alloc();
    if(panel_backend().init()!=PresentError::None)throw std::runtime_error("null panel init");
    runtime_init(nullptr,nullptr,staging,bytes);
    constexpr unsigned psram=MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT;
    if(xTaskCreatePinnedToCoreWithCaps(runtime_display,"display",4096,nullptr,1,nullptr,0,psram)!=pdPASS ||
       xTaskCreatePinnedToCoreWithCaps(runtime_store,"display-store",8192,nullptr,1,nullptr,0,psram)!=pdPASS)
        throw std::runtime_error("null display worker allocation");
    runtime_wait_ready();
}
void census_provision_empty(){null_unsupported("provision-empty");}
void census_fetch(const char*){null_unsupported("fetch");}
void census_push(unsigned,unsigned){null_unsupported("push");}
void census_set_clock(unsigned){null_unsupported("clock");}
unsigned lcd_requested_hz() noexcept{return 0;}
unsigned lcd_actual_khz() noexcept{return 0;}
unsigned dma_generation() noexcept{return 0;}
unsigned dma_bounces() noexcept{return 0;}
unsigned dma_errors() noexcept{return 0;}
void r3_stress_start(unsigned,unsigned,unsigned,unsigned){null_unsupported("r3-stress");}
void r3_stress_stop(){}
void r3_stress_status(void (*out)(const char*)){null_unsupported("r3-stress-status",out);}
void r3_stress_dma_start() noexcept{}
void r3_stress_dma_done(int64_t,int64_t) noexcept{}
void r5_hash_start(unsigned,unsigned){null_unsupported("r5-hash");}
void r5_hash_stop(){}
void r5_hash_status(void (*out)(const char*)){null_unsupported("r5-hash-status",out);}
void r5_hash_dma_start() noexcept{}
void r5_hash_dma_done(int64_t,int64_t) noexcept{}
void r5_hash_error(unsigned) noexcept{}
void r4_crypto_guard(unsigned){null_unsupported("r4-guard");}
R4CryptoHealth r4_crypto_health() noexcept{return {};}
void r4_crypto_status(unsigned,void (*out)(const char*)){null_unsupported("r4-crypto",out);}
void r4_gdma_ready_once() noexcept{}
void r4_asset_context(bool) noexcept{}
namespace detail {
void init_te(){}
int te_gpio() noexcept{return -1;}
bool drain_dma(){return true;}
}
}
