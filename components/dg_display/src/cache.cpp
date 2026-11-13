#include "dg_display/cache.h"
#include <cstring>
#ifdef ESP_PLATFORM
#include "psa/crypto.h"
#include "dg_display/r4_crypto.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <algorithm>
#include <new>
#include <atomic>
#include <cstdio>
#include "esp_timer.h"
#else
#include <openssl/sha.h>
#endif
namespace dg_display {
#ifdef ESP_PLATFORM
namespace {
constexpr size_t HashChunk=4096;
uint8_t* hash_scratch=nullptr;
TaskHandle_t hash_owner=nullptr;
std::atomic<uint64_t> hash_calls{0},hash_bytes{0},hash_us{0},hash_max_us{0},large_calls{0},large_us{0},large_max_us{0};
}
void initialize_asset_hash(){
    configASSERT(!hash_scratch);
    hash_scratch=static_cast<uint8_t*>(heap_caps_aligned_alloc(64,HashChunk,MALLOC_CAP_INTERNAL|MALLOC_CAP_DMA|MALLOC_CAP_8BIT));
    if(!hash_scratch)throw std::bad_alloc();
}
void asset_hash_status(void (*out)(const char*)){
    char b[600];std::snprintf(b,sizeof(b),"\nDISPLAY_R4_HASH {\"chunk\":4096,\"scratch_internal_dma\":%s,\"calls\":%llu,\"bytes\":%llu,\"duration_us\":%llu,\"max_us\":%llu,\"asset306917_calls\":%llu,\"asset306917_us\":%llu,\"asset306917_max_us\":%llu}\n",hash_scratch?"true":"false",(unsigned long long)hash_calls.load(),(unsigned long long)hash_bytes.load(),(unsigned long long)hash_us.load(),(unsigned long long)hash_max_us.load(),(unsigned long long)large_calls.load(),(unsigned long long)large_us.load(),(unsigned long long)large_max_us.load());out(b);
}
#else
void initialize_asset_hash(){}
#endif
bool valid_hash(const char* h) noexcept {if(!h)return false;for(unsigned i=0;i<64;++i)if(!h[i] || !((h[i]>='0' && h[i]<='9') || (h[i]>='a' && h[i]<='f')))return false;return h[64]==0;}
bool digest(const uint8_t* bytes,size_t count,char hex[65]) noexcept {
    uint8_t output[32];
#ifdef ESP_PLATFORM
    configASSERT(hash_scratch);
    const auto caller=xTaskGetCurrentTaskHandle();
    if(!hash_owner)hash_owner=caller;
    configASSERT(hash_owner==caller);
    R4AssetScope asset_scope;
    const auto started=esp_timer_get_time();
    psa_hash_operation_t operation=PSA_HASH_OPERATION_INIT;
    auto status=psa_hash_setup(&operation,PSA_ALG_SHA_256);
    for(size_t at=0;status==PSA_SUCCESS && at<count;){
        const auto chunk=std::min(HashChunk,count-at);
        std::memcpy(hash_scratch,bytes+at,chunk);
        status=psa_hash_update(&operation,hash_scratch,chunk);
        at+=chunk;
    }
    size_t written=0;
    if(status==PSA_SUCCESS)status=psa_hash_finish(&operation,output,sizeof(output),&written);
    psa_hash_abort(&operation);
    const uint64_t elapsed=esp_timer_get_time()-started;
    ++hash_calls;hash_bytes.fetch_add(count);hash_us.fetch_add(elapsed);hash_max_us=std::max(hash_max_us.load(),elapsed);
    if(count==306917){++large_calls;large_us.fetch_add(elapsed);large_max_us=std::max(large_max_us.load(),elapsed);}
    if(status!=PSA_SUCCESS || written!=32)return false;
#else
    if(!SHA256(bytes,count,output))return false;
#endif
    const char* alphabet="0123456789abcdef";for(unsigned i=0;i<32;++i){hex[2*i]=alphabet[output[i]>>4];hex[2*i+1]=alphabet[output[i]&15];}hex[64]=0;return true;
}
const char* error_name(Error e) noexcept {
    switch(e){case Error::None:return "none";case Error::Malformed:return "malformed";case Error::Version:return "unsupported-version";case Error::StaleEpoch:return "stale-epoch";case Error::StaleSequence:return "stale-sequence";case Error::IdReuse:return "id-reuse";case Error::NotLoaded:return "not-loaded";case Error::InUse:return "in-use";case Error::RamBudget:return "ram_budget";case Error::FlashFull:return "flash_full";case Error::Fetch:return "fetch";case Error::Hash:return "hash";case Error::InvalidDgi:return "invalid-dgi";case Error::QueueFull:return "queue-full";case Error::Expired:return "expired";case Error::Cancelled:return "cancelled";case Error::StorageUnavailable:return "storage_unavailable";case Error::CursorChanged:return "cursor-changed";case Error::CatalogFull:return "catalog-full";}
    return "malformed";
}
namespace {
bool retain(void* context,Pin kind){auto& e=*static_cast<Entry*>(context);auto& n=e.pins[unsigned(kind)];if(!e.valid || n==UINT16_MAX)return false;++n;if(e.access_clock)e.last_used=++*e.access_clock;return true;}
void release(void* context,Pin kind){auto& n=static_cast<Entry*>(context)->pins[unsigned(kind)];if(n)--n;}
bool decode(void* context,unsigned y,unsigned x,unsigned n,Pixel* out,size_t cap){auto* e=static_cast<Entry*>(context);return e->valid && e->decoder.crop(y,x,n,out,cap);}
}
Entry* Cache::find(const char* hash) noexcept {for(auto& e:entries)if(e.hash[0] && !std::strcmp(e.hash,hash))return &e;return nullptr;}
Error Cache::reserve(const char* hash,size_t bytes,Entry*& result) noexcept {
    result=nullptr;if(!valid_hash(hash) || !bytes || bytes>524288)return Error::Malformed;
    if(auto* old=find(hash)){if(old->valid){old->last_used=++clock;result=old;return Error::None;}return Error::InUse;}
    Entry* empty=nullptr;for(auto& e:entries)if(!e.hash[0]){empty=&e;break;}
    if(!empty)return Error::RamBudget;
    // Reject impossible requests before any arena allocation or eviction.
    if(!arena.budget() || bytes+64>arena.budget())return Error::RamBudget;
    while(arena.largest()<((bytes+15)&~size_t(15))){
        Entry* victim=nullptr;for(auto& e:entries)if(e.valid && !e.references() && (!victim || e.last_used<victim->last_used))victim=&e;
        if(!victim)return Error::RamBudget;
        arena.release(victim->bytes);*victim=Entry{};++revision;
    }
    void* p=arena.allocate(bytes);if(!p)return Error::RamBudget;
    std::memcpy(empty->hash,hash,65);empty->bytes=static_cast<uint8_t*>(p);empty->size=bytes;empty->pins[unsigned(Pin::Inflight)]=1;empty->last_used=++clock;result=empty;++revision;return Error::None;
}
Error Cache::finish(Entry& e,const Image& image,bool session) noexcept {
    if(!e.bytes || !e.pins[unsigned(Pin::Inflight)])return Error::Malformed;
    e.decoder=image;e.valid=true;e.access_clock=&clock;e.handle={&e,image.width(),image.height(),decode,&e,retain,release};e.pins[unsigned(Pin::Inflight)]=0;
    if(session)e.pins[unsigned(Pin::Session)]=1;
    e.last_used=++clock;++revision;return Error::None;
}
void Cache::abort(Entry& e) noexcept {if(e.bytes)arena.release(e.bytes);e=Entry{};++revision;}
Error Cache::erase(const char* hash) noexcept {if(!valid_hash(hash))return Error::Malformed;auto* e=find(hash);if(!e)return Error::None;if(e->active_references())return Error::InUse;arena.release(e->bytes);*e=Entry{};++revision;return Error::None;}
bool Cache::pin(Entry& e,Pin kind) noexcept {auto& n=e.pins[unsigned(kind)];if(!e.valid || n==UINT16_MAX)return false;++n;e.last_used=++clock;return true;}
void Cache::unpin(Entry& e,Pin kind) noexcept {auto& n=e.pins[unsigned(kind)];if(n)--n;}
size_t Cache::reserved()const noexcept {size_t n=0;for(const auto& e:entries)if(e.bytes && !e.valid)n+=arena.charge(e.bytes);return n;}
size_t Cache::pinned()const noexcept {size_t n=0;for(const auto& e:entries)if(e.bytes && e.references())n+=arena.charge(e.bytes);return n;}
size_t Cache::evictable()const noexcept {size_t n=0;for(const auto& e:entries)if(e.valid && !e.references())n+=arena.charge(e.bytes);return n;}
unsigned Cache::count()const noexcept {unsigned n=0;for(const auto& e:entries)n+=e.valid;return n;}
}
