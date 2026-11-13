#define MBEDTLS_DECLARE_PRIVATE_IDENTIFIERS
#include "dg_display/r5_hash.h"
#include "dg_display/r4_crypto.h"
#include "psa/crypto.h"
#include "mbedtls/private/sha256.h"
#include "sha/sha_core.h"
#include "esp_crypto_shared_gdma.h"
#include "esp_heap_caps.h"
#include "esp_memory_utils.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <atomic>
#include <algorithm>
#include <cstring>
#include <cstdio>
#include <stdexcept>
namespace {
constexpr unsigned Bytes=306917,Chunk=8192,InternalChunk=4096,Psram=MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT;
constexpr uint8_t Expected[32]={0x20,0x4a,0x08,0x84,0xfd,0xd6,0xba,0x24,0xab,0x62,0xb1,0x73,0x25,0x58,0x5c,0x70,0xf1,0x11,0x6e,0x1a,0x57,0x8a,0xab,0x77,0xbc,0x7f,0x2d,0x84,0x0a,0xae,0xe0,0xc2};
std::atomic<bool> running{false},exited{true},observing{false},hash_active{false},sha_active{false},lcd_active{false};
std::atomic<int64_t> began{0},ended{0},hash_begin{0},hash_end{0},sha_begin{0},sha_end{0};
std::atomic<uint64_t> hashes{0},hashed_bytes{0},hash_wall{0},hash_max{0},bursts{0},sha_calls{0},gdma_starts{0},psram_bytes{0},internal_bytes{0},sha_failures{0},hash_failures{0},mismatches{0},gate_failures{0},gate_wait{0},gate_wait_max{0},gate_hold{0},gate_hold_max{0},hash_overlaps{0},sha_overlaps{0};
std::atomic<unsigned> dma_mask{0};
uint8_t *source=nullptr,*scratch=nullptr;unsigned core=0,mode=0;
bool gated(){return mode==1 || mode==2;}
bool take(int64_t& at){const auto before=esp_timer_get_time();if(!dg_display::runtime_hash_gate_take()){++gate_failures;return false;}at=esp_timer_get_time();const auto wait=uint64_t(at-before);gate_wait.fetch_add(wait);gate_wait_max=std::max(gate_wait_max.load(),wait);return true;}
void give(int64_t at){const auto held=uint64_t(esp_timer_get_time()-at);gate_hold.fetch_add(held);gate_hold_max=std::max(gate_hold_max.load(),held);dg_display::runtime_hash_gate_give();}
int hash_one(uint8_t output[32]){
    size_t written=0;int64_t token=0;int status=0;
    if(mode<=1){if(mode==1 && !take(token))return -1;status=psa_hash_compute(PSA_ALG_SHA_256,source,Bytes,output,32,&written);if(mode==1){give(token);vTaskDelay(1);}return status==PSA_SUCCESS && written==32?0:-1;}
    if(mode==3)return mbedtls_sha256(source,Bytes,output,0);
    psa_hash_operation_t operation=PSA_HASH_OPERATION_INIT;status=psa_hash_setup(&operation,PSA_ALG_SHA_256);
    for(unsigned offset=0;status==PSA_SUCCESS && offset<Bytes;){const auto count=std::min(mode==4?InternalChunk:Chunk,Bytes-offset);const uint8_t* input=source+offset;
        if(mode==4){std::memcpy(scratch,input,count);input=scratch;}
        if(mode==2 && !take(token)){status=PSA_ERROR_HARDWARE_FAILURE;break;}
        status=psa_hash_update(&operation,input,count);if(mode==2){give(token);vTaskDelay(1);}offset+=count;
    }
    if(status==PSA_SUCCESS){if(mode==2 && !take(token))status=PSA_ERROR_HARDWARE_FAILURE;else{status=psa_hash_finish(&operation,output,32,&written);if(mode==2){give(token);vTaskDelay(1);}}}
    psa_hash_abort(&operation);return status==PSA_SUCCESS && written==32?0:-1;
}
void worker(void*){
    began=esp_timer_get_time();
    while(running.load()){
        const auto burst=esp_timer_get_time();
        do{uint8_t output[32];const auto start=esp_timer_get_time();hash_begin=start;hash_end=0;hash_active=true;if(lcd_active.load())dma_mask.fetch_or(1u);int status=hash_one(output);const auto finish=esp_timer_get_time();hash_end=finish;hash_active=false;
            const auto wall=uint64_t(finish-start);hash_wall.fetch_add(wall);hash_max=std::max(hash_max.load(),wall);
            if(status){++hash_failures;running=false;break;}if(std::memcmp(output,Expected,32)){++mismatches;running=false;break;}++hashes;hashed_bytes.fetch_add(Bytes);
        }while(running.load() && esp_timer_get_time()-burst<20000);
        ++bursts;const auto remaining=21000-(esp_timer_get_time()-burst);vTaskDelay(std::max(TickType_t(1),pdMS_TO_TICKS(unsigned(std::max(int64_t(0),remaining))/1000)));
    }
    ended=esp_timer_get_time();exited=true;vTaskDeleteWithCaps(nullptr);
}
unsigned active_mask(){return (hash_active.load()?1u:0u)|(sha_active.load()?2u:0u);}
}
extern "C" int __real_esp_sha_dma(esp_sha_type,const void*,uint32_t,const void*,uint32_t,bool);
extern "C" int __wrap_esp_sha_dma(esp_sha_type type,const void* input,uint32_t len,const void* buf,uint32_t blen,bool first){
    const int gate=dg_display::r4_crypto_gate_begin(input,len,buf,blen);if(gate<0)return -1;
    if(!observing.load()){const int result=__real_esp_sha_dma(type,input,len,buf,blen,first);dg_display::r4_crypto_gate_end(gate>0);return result;}
    ++sha_calls;sha_begin=esp_timer_get_time();sha_end=0;sha_active=true;if(lcd_active.load())dma_mask.fetch_or(2u);int result=__real_esp_sha_dma(type,input,len,buf,blen,first);sha_end=esp_timer_get_time();sha_active=false;if(result)++sha_failures;dg_display::r4_crypto_gate_end(gate>0);return result;
}
extern "C" esp_err_t __real_esp_crypto_shared_gdma_start_axi_ahb(const crypto_dma_desc_t*,const crypto_dma_desc_t*,crypto_dma_user_t);
extern "C" esp_err_t __wrap_esp_crypto_shared_gdma_start_axi_ahb(const crypto_dma_desc_t* input,const crypto_dma_desc_t* output,crypto_dma_user_t user){
    if(observing.load() && user==CRYPTO_DMA_USER_SHA){++gdma_starts;for(auto* descriptor=input;descriptor;descriptor=descriptor->next){if(esp_ptr_external_ram(descriptor->buffer))psram_bytes.fetch_add(descriptor->dw0.length);else internal_bytes.fetch_add(descriptor->dw0.length);}}
    return __real_esp_crypto_shared_gdma_start_axi_ahb(input,output,user);
}
namespace dg_display {
void r5_hash_start(unsigned requested_core,unsigned requested_mode){
    if(running || !exited || source)throw std::runtime_error("hash stressor already allocated");
    if(requested_core>1 || requested_mode>4)throw std::runtime_error("hash core0|1 mode0..4");
    source=static_cast<uint8_t*>(heap_caps_aligned_alloc(64,(Bytes+63)&~63u,Psram));if(requested_mode==4)scratch=static_cast<uint8_t*>(heap_caps_aligned_alloc(64,InternalChunk,MALLOC_CAP_INTERNAL|MALLOC_CAP_DMA|MALLOC_CAP_8BIT));
    if(!source || (requested_mode==4 && !scratch)){heap_caps_free(source);heap_caps_free(scratch);source=scratch=nullptr;throw std::bad_alloc();}
    for(unsigned i=0;i<Bytes;++i)source[i]=uint8_t(i*17+(i>>8));
    core=requested_core;mode=requested_mode;
    hashes=0;hashed_bytes=0;hash_wall=0;hash_max=0;bursts=0;sha_calls=0;gdma_starts=0;psram_bytes=0;internal_bytes=0;sha_failures=0;hash_failures=0;mismatches=0;gate_failures=0;gate_wait=0;gate_wait_max=0;gate_hold=0;gate_hold_max=0;hash_overlaps=0;sha_overlaps=0;began=0;ended=0;hash_begin=0;hash_end=0;sha_begin=0;sha_end=0;dma_mask=0;hash_active=false;sha_active=false;lcd_active=false;exited=false;running=true;observing=true;
    if(xTaskCreatePinnedToCoreWithCaps(worker,"r5-sha",8192,nullptr,1,nullptr,int(core),Psram)!=pdPASS){running=false;exited=true;observing=false;heap_caps_free(source);heap_caps_free(scratch);source=scratch=nullptr;throw std::bad_alloc();}
}
void r5_hash_stop(){running=false;const auto start=esp_timer_get_time();while(!exited){if(esp_timer_get_time()-start>3000000)throw std::runtime_error("hash stop timeout");vTaskDelay(1);}observing=false;heap_caps_free(source);heap_caps_free(scratch);source=scratch=nullptr;}
void r5_hash_dma_start() noexcept{if(observing.load()){dma_mask=0;lcd_active=true;dma_mask.fetch_or(active_mask());}else{dma_mask=0;lcd_active=false;}}
void r5_hash_dma_done(int64_t,int64_t) noexcept{if(!observing.load())return;dma_mask.fetch_or(active_mask());lcd_active=false;const auto mask=dma_mask.load();if(mask&1)++hash_overlaps;if(mask&2)++sha_overlaps;}
void r5_hash_error(unsigned index) noexcept{if(!began.load())return;std::printf("\nDISPLAY_HASH_DMA_ERROR {\"dma_index\":%u,\"mask\":%u,\"core\":%u,\"mode\":%u,\"hashes\":%llu,\"sha_calls\":%llu,\"gdma_starts\":%llu,\"psram_bytes\":%llu,\"hash_window\":[%lld,%lld],\"sha_api_window\":[%lld,%lld]}\n",index,dma_mask.load(),core,mode,(unsigned long long)hashes.load(),(unsigned long long)sha_calls.load(),(unsigned long long)gdma_starts.load(),(unsigned long long)psram_bytes.load(),(long long)hash_begin.load(),(long long)hash_end.load(),(long long)sha_begin.load(),(long long)sha_end.load());}
void r5_hash_status(void (*out)(const char*)){char b[1500];const auto stop=ended.load()?ended.load():esp_timer_get_time();std::snprintf(b,sizeof(b),"\nDISPLAY_R5_HASH {\"running\":%s,\"allocations_live\":%s,\"core\":%u,\"mode\":%u,\"gated\":%s,\"buffer_bytes\":%u,\"chunk_bytes\":%u,\"priority\":1,\"stack_bytes\":8192,\"elapsed_us\":%lld,\"hashes\":%llu,\"hashed_bytes\":%llu,\"hash_wall_us\":%llu,\"hash_max_us\":%llu,\"bursts\":%llu,\"sha_calls\":%llu,\"gdma_starts\":%llu,\"psram_bytes\":%llu,\"internal_bytes\":%llu,\"sha_failures\":%llu,\"hash_failures\":%llu,\"mismatches\":%llu,\"gate_failures\":%llu,\"gate_wait_us\":%llu,\"gate_wait_max_us\":%llu,\"gate_hold_us\":%llu,\"gate_hold_max_us\":%llu,\"hash_overlap_stripes\":%llu,\"sha_overlap_stripes\":%llu}\n",running?"true":"false",source?"true":"false",core,mode,gated()?"true":"false",Bytes,mode==2?Chunk:mode==4?InternalChunk:Bytes,(long long)(began.load()?stop-began.load():0),(unsigned long long)hashes.load(),(unsigned long long)hashed_bytes.load(),(unsigned long long)hash_wall.load(),(unsigned long long)hash_max.load(),(unsigned long long)bursts.load(),(unsigned long long)sha_calls.load(),(unsigned long long)gdma_starts.load(),(unsigned long long)psram_bytes.load(),(unsigned long long)internal_bytes.load(),(unsigned long long)sha_failures.load(),(unsigned long long)hash_failures.load(),(unsigned long long)mismatches.load(),(unsigned long long)gate_failures.load(),(unsigned long long)gate_wait.load(),(unsigned long long)gate_wait_max.load(),(unsigned long long)gate_hold.load(),(unsigned long long)gate_hold_max.load(),(unsigned long long)hash_overlaps.load(),(unsigned long long)sha_overlaps.load());out(b);}
}
