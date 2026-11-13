#pragma once
#include "arena.h"
#include "scene.h"
namespace dg_display {
bool valid_hash(const char* text) noexcept;
// ESP asset validation is serialized by the store owner. Allocate once at init.
void initialize_asset_hash();
#ifdef ESP_PLATFORM
void asset_hash_status(void (*out)(const char*));
#endif
bool digest(const uint8_t* bytes,size_t count,char hex[65]) noexcept;
enum class Error:uint8_t {None,Malformed,Version,StaleEpoch,StaleSequence,IdReuse,NotLoaded,InUse,RamBudget,FlashFull,Fetch,Hash,InvalidDgi,QueueFull,Expired,Cancelled,StorageUnavailable,CursorChanged,CatalogFull};
const char* error_name(Error e) noexcept;
struct Entry {
    char hash[65]{};uint8_t* bytes=nullptr;size_t size=0;uint64_t last_used=0;uint64_t* access_clock=nullptr;
    uint16_t pins[5]{};bool valid=false,flash_known=false,persisted=false;
    Image decoder;ImageHandle handle;
    unsigned references()const noexcept{unsigned n=0;for(auto p:pins)n+=p;return n;}
    unsigned active_references()const noexcept{return references()-pins[4];}
};
struct Cache {
    Arena arena;Entry entries[128]{};uint64_t clock=0,revision=1;
    Entry* find(const char* hash) noexcept;
    Error reserve(const char* hash,size_t bytes,Entry*& result) noexcept;
    Error finish(Entry& entry,const Image& decoded,bool session) noexcept;
    void abort(Entry& entry) noexcept;
    Error erase(const char* hash) noexcept;
    bool pin(Entry& entry,Pin kind) noexcept;
    void unpin(Entry& entry,Pin kind) noexcept;
    size_t reserved()const noexcept;
    size_t pinned()const noexcept;
    size_t evictable()const noexcept;
    unsigned count()const noexcept;
};
}
