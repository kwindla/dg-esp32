#include "dg_display/r4_crypto.h"
#include "dg_display/r5_hash.h"
#include "esp_private/gdma.h"
#include "esp_crypto_dma.h"
#include "esp_memory_utils.h"
#include "aes/esp_aes.h"
#include "driver/spi_master.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "soc/gdma_struct.h"
#include <atomic>
#include <cstring>
#include <cstdio>
#include <algorithm>
#include <stdexcept>
// Application-owned buffered USB writer: holds the same stdout lock as IDF logs.
// Called on app-session, never a transport/audio callback.
bool app_console_line(const char* text) noexcept;
namespace {
constexpr unsigned Channels=10,Paths=12;
struct Channel {gdma_channel_handle_t handle=nullptr;bool tx=false;int peripheral=-1;};
Channel channels[Channels];
gdma_channel_handle_t lcd_tx=nullptr;
std::atomic<unsigned> guard_mode{1},last_priority{0},priority_applies{0},lcd_connects{0},coverage_lost{0},gate_failures{0};
std::atomic<bool> ready_dumped{false};
std::atomic<TaskHandle_t> asset_owner{nullptr};
struct Path {TaskHandle_t task=nullptr;char name[24]{};int peripheral=-1;bool asset=false;unsigned core=0;uintptr_t caller=0;std::atomic<uint64_t> calls{0},input_psram{0},input_internal{0},output_psram{0},output_internal{0},failures{0};};
Path paths[Paths];std::atomic<unsigned> path_count{0};
Channel* channel(gdma_channel_handle_t handle){for(auto& c:channels)if(c.handle==handle)return &c;return nullptr;}
void remember(gdma_channel_handle_t h,bool tx){if(!h)return;for(auto& c:channels)if(!c.handle){c.handle=h;c.tx=tx;return;}++coverage_lost;}
unsigned observe(Channel& c,uint32_t descriptor,uintptr_t caller){
    auto task=xTaskGetCurrentTaskHandle();const char* name=pcTaskGetName(task);const bool asset=asset_owner.load()==task;unsigned index=Paths;
    for(unsigned i=0;i<path_count.load();++i)if(!std::strcmp(paths[i].name,name) && paths[i].peripheral==c.peripheral && paths[i].asset==asset){index=i;break;}
    if(index==Paths){const auto n=path_count.load();if(n>=Paths){++coverage_lost;return Paths;}index=n;auto& p=paths[n];p.task=task;p.peripheral=c.peripheral;p.asset=asset;p.core=xPortGetCoreID();p.caller=caller;std::snprintf(p.name,sizeof(p.name),"%s",pcTaskGetName(task));path_count=n+1;}
    auto& p=paths[index];++p.calls;unsigned traversed=0;
    for(auto* d=reinterpret_cast<const crypto_dma_desc_t*>(uintptr_t(descriptor));d;d=d->next){
        if(++traversed>4096){++coverage_lost;break;}
        const auto count=c.tx?d->dw0.length:d->dw0.size;const bool external=esp_ptr_external_ram(d->buffer);
        if(c.tx){if(external)p.input_psram.fetch_add(count);else p.input_internal.fetch_add(count);}else{if(external)p.output_psram.fetch_add(count);else p.output_internal.fetch_add(count);}
    }
    return index;
}
bool crypto_peripheral(int id){return id==SOC_GDMA_TRIG_PERIPH_SHA0 || id==SOC_GDMA_TRIG_PERIPH_AES0;}
}
extern "C" esp_err_t __real_gdma_new_ahb_channel(const gdma_channel_alloc_config_t*,gdma_channel_handle_t*,gdma_channel_handle_t*);
extern "C" esp_err_t __wrap_gdma_new_ahb_channel(const gdma_channel_alloc_config_t* config,gdma_channel_handle_t* tx,gdma_channel_handle_t* rx){auto result=__real_gdma_new_ahb_channel(config,tx,rx);if(result==ESP_OK){if(tx)remember(*tx,true);if(rx)remember(*rx,false);}return result;}
extern "C" esp_err_t __real_gdma_connect(gdma_channel_handle_t,gdma_trigger_t);
extern "C" esp_err_t __wrap_gdma_connect(gdma_channel_handle_t h,gdma_trigger_t trigger){
    auto result=__real_gdma_connect(h,trigger);if(result!=ESP_OK)return result;auto* c=channel(h);if(c)c->peripheral=trigger.instance_id;
    if(c && c->tx && trigger.instance_id==SOC_GDMA_TRIG_PERIPH_SPI2){lcd_tx=h;++lcd_connects;last_priority=0;}
    if(h==lcd_tx && guard_mode.load()==1){result=gdma_set_priority(h,1);if(result==ESP_OK){last_priority=1;++priority_applies;}}
    return result;
}
extern "C" esp_err_t __real_gdma_start(gdma_channel_handle_t,uint32_t);
extern "C" esp_err_t __wrap_gdma_start(gdma_channel_handle_t h,uint32_t descriptor){auto* c=channel(h);unsigned index=Paths;if(c && crypto_peripheral(c->peripheral))index=observe(*c,descriptor,reinterpret_cast<uintptr_t>(__builtin_return_address(0)));auto result=__real_gdma_start(h,descriptor);if(result!=ESP_OK && index<Paths)++paths[index].failures;return result;}
namespace dg_display {
R4CryptoHealth r4_crypto_health() noexcept {
    R4CryptoHealth h;h.lcd_tx_captured=lcd_tx!=nullptr;
    if(lcd_tx && gdma_get_channel_id(lcd_tx,&h.lcd_tx_id)==ESP_OK && h.lcd_tx_id>=0 && h.lcd_tx_id<5)
        h.tx_pri=GDMA.channel[h.lcd_tx_id].out.pri.tx_pri;
    h.priority_applies=priority_applies.load();h.last_priority_written=last_priority.load();
    h.coverage_lost=coverage_lost.load();h.gate_failures=gate_failures.load();
    for(unsigned i=0;i<path_count.load();++i){auto& p=paths[i];
        if(p.peripheral==SOC_GDMA_TRIG_PERIPH_SHA0){h.sha_psram_input+=p.input_psram.load();h.sha_internal_input+=p.input_internal.load();}
        if(p.peripheral==SOC_GDMA_TRIG_PERIPH_AES0){h.aes_psram_input+=p.input_psram.load();h.aes_internal_input+=p.input_internal.load();}
        h.psram_output_capacity+=p.output_psram.load();h.internal_output_capacity+=p.output_internal.load();
    }return h;
}
void r4_gdma_ready_once() noexcept {
    if(!runtime_crypto_gate_ready() || ready_dumped.exchange(true))return;
    // S3 has five AHB GDMA pairs; these are actual peripheral-select registers.
    char b[768];size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_R4_GDMA_READY {\"device_us\":%lld,\"pairs\":[",(long long)esp_timer_get_time());
    for(unsigned i=0;i<5;++i)at+=std::snprintf(b+at,sizeof(b)-at,"%s{\"pair\":%u,\"tx_peri_sel\":%u,\"rx_peri_sel\":%u,\"tx_priority\":%u,\"rx_priority\":%u}",i?",":"",i,unsigned(GDMA.channel[i].out.peri_sel.sel),unsigned(GDMA.channel[i].in.peri_sel.sel),unsigned(GDMA.channel[i].out.pri.tx_pri),unsigned(GDMA.channel[i].in.pri.rx_pri));
    std::snprintf(b+at,sizeof(b)-at,"]}\n");
    if(!app_console_line(b)) {
        ++gate_failures;
        ESP_LOGE("display-gdma","Readiness console write failed");
    }
}
void r4_asset_context(bool active) noexcept{asset_owner=active?xTaskGetCurrentTaskHandle():nullptr;}
int r4_crypto_gate_begin(const void* input,size_t n,const void* extra,size_t m) noexcept{
    if(!runtime_crypto_gate_ready() || guard_mode.load()!=2 || (!(n && esp_ptr_external_ram(input)) && !(m && esp_ptr_external_ram(extra))))return false;
    if(!runtime_crypto_gate_take()){++gate_failures;return -1;}return 1;
}
void r4_crypto_gate_end(bool held) noexcept{if(held)runtime_hash_gate_give();}
void r4_crypto_guard(unsigned mode){if(mode>2)throw std::runtime_error("crypto guard0=none1=LCDpriority2=gate");guard_mode=mode;if(lcd_tx){const auto priority=mode==1?1u:0u;if(gdma_set_priority(lcd_tx,priority)!=ESP_OK)throw std::runtime_error("GDMA priority set failed");last_priority=priority;if(mode==1)++priority_applies;}}
void r4_crypto_status(unsigned start,void (*out)(const char*)){char b[1800];const auto health=r4_crypto_health();const auto total=path_count.load();size_t at=std::snprintf(b,sizeof(b),"\nDISPLAY_R4_CRYPTO {\"guard\":%u,\"lcd_tx_captured\":%s,\"lcd_tx_id\":%d,\"tx_pri\":%d,\"last_priority_written\":%u,\"priority_applies\":%u,\"lcd_connects\":%u,\"coverage_lost\":%u,\"gate_failures\":%u,\"total\":%u,\"start\":%u,\"items\":[",guard_mode.load(),lcd_tx?"true":"false",health.lcd_tx_id,health.tx_pri,last_priority.load(),priority_applies.load(),lcd_connects.load(),coverage_lost.load(),gate_failures.load(),total,start);
    for(unsigned i=start;i<total && i<start+3;++i){auto& p=paths[i];at+=std::snprintf(b+at,sizeof(b)-at,"%s{\"index\":%u,\"task\":\"%s\",\"core\":%u,\"peripheral\":%d,\"asset\":%s,\"caller_pc\":%u,\"starts\":%llu,\"input_psram\":%llu,\"input_internal\":%llu,\"output_psram_capacity\":%llu,\"output_internal_capacity\":%llu,\"failures\":%llu}",i==start?"":",",i,p.name,p.core,p.peripheral,p.asset?"true":"false",unsigned(p.caller),(unsigned long long)p.calls.load(),(unsigned long long)p.input_psram.load(),(unsigned long long)p.input_internal.load(),(unsigned long long)p.output_psram.load(),(unsigned long long)p.output_internal.load(),(unsigned long long)p.failures.load());}
    std::snprintf(b+at,sizeof(b)-at,"]}\n");out(b);
}
}
extern "C" int __real_esp_aes_process_dma(esp_aes_context*,const unsigned char*,unsigned char*,size_t,uint8_t*);
extern "C" int __wrap_esp_aes_process_dma(esp_aes_context* ctx,const unsigned char* in,unsigned char* out,size_t n,uint8_t* stream){const int token=dg_display::r4_crypto_gate_begin(in,n,out,n);if(token<0)return -1;const bool held=token>0;const int result=__real_esp_aes_process_dma(ctx,in,out,n,stream);dg_display::r4_crypto_gate_end(held);return result;}
extern "C" int __real_esp_aes_process_dma_gcm(esp_aes_context*,const unsigned char*,unsigned char*,size_t,const unsigned char*,size_t);
extern "C" int __wrap_esp_aes_process_dma_gcm(esp_aes_context* ctx,const unsigned char* in,unsigned char* out,size_t n,const unsigned char* aad,size_t alen){const bool psram_aad=alen && esp_ptr_external_ram(aad);const int token=dg_display::r4_crypto_gate_begin(psram_aad?aad:in,psram_aad?alen:n,out,n);if(token<0)return -1;const bool held=token>0;const int result=__real_esp_aes_process_dma_gcm(ctx,in,out,n,aad,alen);dg_display::r4_crypto_gate_end(held);return result;}
