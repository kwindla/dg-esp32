#pragma once
#include "engine.h"
#include "duo_commit.h"
#include "duo_admission.h"
#include "flash_store.h"
#include <algorithm>
#include <cstddef>
#include <cstring>
namespace dg_display {
struct DuoAsset {const char* hash;unsigned bytes;};
struct DuoMemory {size_t used=0,largest=0;unsigned pinned=0;};
// The store as an install finds it: the flash catalog and the RAM arena.
struct DuoStoreRoom {
    const FlashItem* inventory=nullptr;size_t inventory_slots=0;  // An unused slot has an empty hash.
    unsigned catalog_files=0;
    size_t flash_used=0,flash_temporary=0,flash_capacity=0,flash_free=0,flash_admissible=0,arena_budget=0;
};
// Whether the store can take every asset of an install. No device is touched:
// duo_admit() calls this under the engine lock, with the store's own numbers.
//   one asset        at most 256 kB
//   the catalog      at most 96 files, counting the ones still to fetch
//   the arena        the whole set, each rounded up to 16 bytes plus a 16-byte header, in 3/4 of it
//   the flash        what is used, what is to fetch, and the largest of those once more for its
//                    temporary copy, in 3/4 of the capacity; 16 kB free beyond what is to fetch
inline Error duo_store_admission(const DuoAsset* assets,size_t count,const DuoStoreRoom& room){
    size_t missing_bytes=0,largest=0,charged=0;unsigned missing_count=0;
    for(size_t n=0;n<count;++n){const auto& a=assets[n];if(a.bytes>262144)return Error::RamBudget;
        charged+=((a.bytes+15u)&~size_t(15))+16;
        bool found=false;for(size_t i=0;i<room.inventory_slots;++i)if(!std::strcmp(room.inventory[i].hash,a.hash)){found=true;break;}
        if(!found){missing_bytes+=a.bytes;largest=std::max(largest,size_t(a.bytes));++missing_count;}
    }
    if(count>96 || room.catalog_files+missing_count>96)return Error::CatalogFull;
    if(charged>room.arena_budget*3/4)return Error::RamBudget;
    if(room.flash_used+missing_bytes+largest+room.flash_temporary>room.flash_capacity*3/4 ||
       (missing_bytes && (room.flash_free<missing_bytes+16384 || largest>room.flash_admissible)))return Error::FlashFull;
    return Error::None;
}
// Only linked in CONFIG_DG_DUO images. Boot owner serializes these idle calls.
Error duo_prepare();
bool duo_session_start();
Error duo_inspect(const DuoAsset&);
Error duo_admit(const DuoAsset*,size_t);
Error duo_restore(const DuoAsset&);
Error duo_fetch(const DuoAsset&,const char* origin);
Error duo_pin(const DuoAsset&);
void duo_unpin(const DuoAsset&)noexcept;
DuoMemory duo_memory();
bool duo_status_screen(bool connecting);
bool duo_scene_post(const Request&,DuoTicket* ticket=nullptr);
DuoPostResult duo_scene_admit(const Request&,DuoTicket* ticket=nullptr);
DuoCommit duo_scene_poll(DuoTicket,uint64_t& revision);
}
