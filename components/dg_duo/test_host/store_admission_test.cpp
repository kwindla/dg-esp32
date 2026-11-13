#include "dg_display/duo_runtime.h"
#include "check.h"
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
using namespace dg_display;
namespace {
// A store like the EchoEar's: a 4 MB filesystem and the 491,520-byte arena.
struct Store {
    FlashItem inventory[128]{};DuoStoreRoom room;unsigned slots=0;
    Store(){room.inventory=inventory;room.inventory_slots=128;room.flash_capacity=4194304;room.flash_free=4194304;
        room.flash_admissible=1048576;room.arena_budget=491520;}
    // A file already in the catalog.
    void have(const char* hash,unsigned bytes,bool valid=true){
        std::strcpy(inventory[slots].hash,hash);inventory[slots].bytes=bytes;inventory[slots].valid=valid;++slots;
        ++room.catalog_files;room.flash_used+=bytes;room.flash_free-=bytes;
    }
    Error admit(const std::vector<DuoAsset>& set)const{return duo_store_admission(set.data(),set.size(),room);}
};
// `count` assets of `bytes` each, with hashes that stay alive.
struct Set {
    std::vector<std::string> names;std::vector<DuoAsset> assets;
    Set(unsigned count,unsigned bytes){names.reserve(count);
        for(unsigned i=0;i<count;++i){names.push_back("asset-"+std::to_string(i));assets.push_back({names.back().c_str(),bytes});}}
};
}
int main(){
    // An empty store takes a small set, and an empty request.
    {Store s;CHECK(s.admit({{"a",1000},{"b",2000}})==Error::None);CHECK(duo_store_admission(nullptr,0,s.room)==Error::None);}
    // One asset is at most 256 kB, whatever room there is.
    {Store s;CHECK(s.admit({{"a",262144}})==Error::None);CHECK(s.admit({{"a",262145}})==Error::RamBudget);}
    // The arena: the whole set, each rounded up to 16 bytes plus 16, in 3/4 of 491,520 = 368,640.
    {Store s;CHECK(s.admit({{"a",184304},{"b",184304}})==Error::None);      // 2 x (184,304 + 16) = 368,640
     CHECK(s.admit({{"a",184305},{"b",184304}})==Error::RamBudget);         // one byte more rounds up to 368,656
     // Assets already in the catalog are charged to the arena all the same: they will be loaded too.
     s.have("a",184305);s.have("b",184304);CHECK(s.admit({{"a",184305},{"b",184304}})==Error::RamBudget);}
    // The catalog: 96 files, counting the ones still to fetch.
    {Store s;Set full(96,100),over(97,100);
     CHECK(s.admit(full.assets)==Error::None);CHECK(s.admit(over.assets)==Error::CatalogFull);}
    {Store s;for(unsigned i=0;i<90;++i)s.have(("other-"+std::to_string(i)).c_str(),10);
     Set six(6,100),seven(7,100);
     CHECK(s.admit(six.assets)==Error::None);CHECK(s.admit(seven.assets)==Error::CatalogFull);}
    // A full catalog still admits a set that is all there already.
    {Store s;Set set(96,100);for(const auto& a:set.assets)s.have(a.hash,a.bytes);
     CHECK(s.room.catalog_files==96 && s.admit(set.assets)==Error::None);}
    // The flash: used + to fetch + the largest once more (its temporary copy) + temporaries, in 3/4 of 4 MB = 3,145,728.
    {Store s;s.room.flash_used=2745728;CHECK(s.admit({{"a",200000}})==Error::None);   // 2,745,728 + 2 x 200,000
     s.room.flash_used=2745729;CHECK(s.admit({{"a",200000}})==Error::FlashFull);
     s.room.flash_used=2745728;s.room.flash_temporary=1;CHECK(s.admit({{"a",200000}})==Error::FlashFull);}
    // Two to fetch: both count, the larger one twice.
    {Store s;s.room.flash_used=2645728;CHECK(s.admit({{"a",200000},{"b",100000}})==Error::None);
     s.room.flash_used=2645729;CHECK(s.admit({{"a",200000},{"b",100000}})==Error::FlashFull);}
    // 16 kB must stay free beyond what is to fetch, and the largest file must be one the filesystem takes.
    {Store s;s.room.flash_free=216384;CHECK(s.admit({{"a",200000}})==Error::None);
     s.room.flash_free=216383;CHECK(s.admit({{"a",200000}})==Error::FlashFull);}
    {Store s;s.room.flash_admissible=200000;CHECK(s.admit({{"a",200000}})==Error::None);
     s.room.flash_admissible=199999;CHECK(s.admit({{"a",200000}})==Error::FlashFull);}
    // With nothing to fetch, neither free space nor the admissible size is asked for.
    {Store s;s.have("a",200000);s.room.flash_free=0;s.room.flash_admissible=0;CHECK(s.admit({{"a",200000}})==Error::None);
     // As today: a store already past 3/4 refuses an install even when every asset is there.
     s.room.flash_used=3145729;CHECK(s.admit({{"a",200000}})==Error::FlashFull);}
    // As today: a catalog entry counts as present whether or not it is valid. install() stops
    // at inspect() for an invalid one; nothing is fetched over it, so no flash is asked for.
    {Store s;s.have("a",200000,false);s.room.flash_free=0;CHECK(s.admit({{"a",200000}})==Error::None);}
    std::puts("duo store admission: asset, arena, catalog and flash limits passed");
}
