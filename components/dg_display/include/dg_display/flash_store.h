#pragma once
#include "cache.h"
namespace dg_display {
// Worker-only filesystem backend. Construction never mounts, formats or writes a partition.
// Board activation requires an explicitly established LittleFS mount.
struct FlashItem {char hash[65]{};uint32_t bytes=0;Error error=Error::None;bool valid=false;};
class FlashStore {
    char root_[160]{};size_t capacity_=0;bool mounted_=false;
    bool path(const char* hash,bool temporary,char* output,size_t capacity)const noexcept;
public:
    enum class Fault:uint8_t {None,Write,Sync,Close,Rename};
    FlashItem items[128]{};unsigned count=0;size_t used=0,temp_bytes=0;uint64_t revision=1,writes=0;
    Fault fault=Fault::None;
    size_t (*free_bytes)()=nullptr; // Optional conservative filesystem-space snapshot.
    void (*checkpoint)(unsigned,size_t)=nullptr; // HIL-only interruption/yield hook.
    bool (*write_allowed)()=nullptr; // PSRAM-stack integration rejects when flash_mmap_remain()!=0.
    Error mount(const char* root,size_t filesystem_capacity,uint8_t* scratch,size_t scratch_capacity,bool cleanup=true) noexcept;
    Error install(const char* hash,const uint8_t* bytes,size_t size) noexcept;
    Error read(const char* hash,uint8_t* bytes,size_t capacity,size_t& size) noexcept;
    Error erase(const char* hash) noexcept;
    FlashItem* find(const char* hash) noexcept;
    bool available()const noexcept{return mounted_;}
    size_t capacity()const noexcept{return capacity_;}
    size_t max_admissible()const noexcept;
};
}
