#include "check.h"
#include "dg_duo/install.h"
#include "dg_duo/animator.h"
#include "dg_display/flash_store.h"
#include "dg_display/duo_admission.h"
#include <array>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <memory>
#include <string>
#include <vector>
#include <unistd.h>
using namespace dg_duo;
using namespace dg_display;
namespace {
struct Fixture:InstallIo {
    std::string root;alignas(16) std::array<uint8_t,491520> arena{};
    std::vector<uint8_t> scratch=std::vector<uint8_t>(524288);
    Cache cache;FakeClock clock;Engine engine{cache,clock};FlashStore store;
    uint64_t sequence=0;unsigned fetches=0,unlocks=0;int fail_fetch=-1;
    bool lock_available=true;DuoPostResult admission;Request attempted;
    explicit Fixture(std::string directory):root(std::move(directory)){CHECK(cache.arena.init(arena.data(),arena.size()));}
    Error prepare()override{return store.mount(root.c_str(),4*1024*1024,scratch.data(),scratch.size());}
    Error admit(const DuoAsset* assets,size_t count)override{
        size_t charged=0;for(size_t n=0;n<count;++n)charged+=((assets[n].bytes+15u)&~size_t(15))+32;
        return count<=96 && charged<=cache.arena.budget()*3/4?Error::None:Error::RamBudget;
    }
    Error inspect(const DuoAsset& a)override{
        auto* item=store.find(a.hash);if(!item)return Error::NotLoaded;
        return !item->valid?item->error:item->bytes==a.bytes?Error::None:Error::Hash;
    }
    Error resident(const DuoAsset& a,const uint8_t* data){
        Entry* entry=nullptr;auto e=cache.reserve(a.hash,a.bytes,entry);if(e!=Error::None || entry->valid)return e;
        std::memcpy(entry->bytes,data,a.bytes);Image image;
        if(!image.open(entry->bytes,entry->size)){cache.abort(*entry);return Error::InvalidDgi;}
        return cache.finish(*entry,image,false);
    }
    Error restore(const DuoAsset& a)override{
        size_t bytes=0;auto e=store.read(a.hash,scratch.data(),scratch.size(),bytes);
        if(e!=Error::None)return e;
        e=resident(a,scratch.data());
        if(e==Error::None){auto* entry=cache.find(a.hash);entry->persisted=entry->flash_known=true;}
        return e;
    }
    Error fetch(const DuoAsset& a,const char*)override{
        if(int(fetches++)==fail_fetch)return Error::Fetch;
        std::ifstream input(std::string(DUO_FIXTURES)+"/"+a.hash+".dgi",std::ios::binary);
        CHECK(input.good());input.read(reinterpret_cast<char*>(scratch.data()),a.bytes);CHECK(size_t(input.gcount())==a.bytes);
        auto e=resident(a,scratch.data());if(e!=Error::None)return e;
        auto* entry=cache.find(a.hash);CHECK(cache.pin(*entry,Pin::Session));
        e=store.install(a.hash,entry->bytes,entry->size);
        if(e==Error::None)entry->persisted=entry->flash_known=true;
        // This request owns one pin, even if persistence failed.
        cache.unpin(*entry,Pin::Session);return e;
    }
    Error pin(const DuoAsset& a)override{
        auto* entry=cache.find(a.hash);
        return entry && entry->persisted && cache.pin(*entry,Pin::Scheduled)?Error::None:Error::NotLoaded;
    }
    void unpin(const DuoAsset& a)noexcept override{auto* e=cache.find(a.hash);CHECK(e);cache.unpin(*e,Pin::Scheduled);}
    bool scene(const Request& batch){
        attempted=batch;
        admission=duo_admit_batch(batch,nullptr,engine,sequence,[&]{return lock_available;},[&]{++unlocks;},[]{return int64_t(0);});
        return admission.admission==DuoAdmission::Accepted;
    }
    void pins(unsigned residency){for(const auto& entry:cache.entries)if(entry.valid){
        CHECK(entry.pins[unsigned(Pin::Scheduled)]==residency);
        CHECK(!entry.pins[unsigned(Pin::Session)] && !entry.pins[unsigned(Pin::Inflight)] && !entry.pins[unsigned(Pin::Rendering)]);
    }}
};
}
int main(){
    auto* temp=std::getenv("TMPDIR");std::string pattern=std::string(temp?temp:".")+"/duo-store-XXXXXX";
    std::vector<char> path(pattern.begin(),pattern.end());path.push_back(0);CHECK(mkdtemp(path.data()));std::string root=path.data();
    DuoAsset assets[96];auto count=install_assets(assets,96);CHECK(count==31);
    auto first=std::make_unique<Fixture>(root);first->fail_fetch=3;
    auto result=install(*first,assets,count,"http://fixture:7874");
    CHECK(result.error==Error::Fetch && !result.pinned && first->store.count==3);first->pins(0);
    first.reset(); // Reboot: lose all RAM/cache/engine state, retain only real files.
    auto reboot=std::make_unique<Fixture>(root);
    result=install(*reboot,assets,count,"");
    CHECK(result.error==Error::NotLoaded && result.missing_origin && result.restored==3 && !reboot->fetches);reboot->pins(0);
    reboot->store.fault=FlashStore::Fault::Rename;
    result=install(*reboot,assets,count,"http://fixture:7874");
    CHECK(result.error==Error::StorageUnavailable && !result.pinned && reboot->store.count==3);reboot->pins(0);
    CHECK(!reboot->store.temp_bytes); // Failed durable install leaves no fabricated catalog entry.
    reboot->store.fault=FlashStore::Fault::None;
    result=install(*reboot,assets,count,"http://fixture:7874");
    CHECK(result.error==Error::None && result.restored==3 && result.fetched==28 && result.pinned==31);
    CHECK(reboot->store.count==31);reboot->pins(1);
    // Native cache ownership: dropping our one failure pin must preserve another owner.
    auto* owned=reboot->cache.find(assets[0].hash);CHECK(reboot->cache.pin(*owned,Pin::Session));CHECK(reboot->cache.pin(*owned,Pin::Session));
    reboot->cache.unpin(*owned,Pin::Session);CHECK(owned->pins[unsigned(Pin::Session)]==1);reboot->cache.unpin(*owned,Pin::Session);
    // Boot order: pending face in the old epoch is cancelled by start_session.
    DuoAnimator animator([](void* p,const Request& q){return static_cast<Fixture*>(p)->scene(q);},reboot.get());
    CHECK(animator.set_active(Character::Angel,12345));CHECK(reboot->engine.occupancy()==1);
    const auto before=reboot->engine.epoch;reboot->engine.start_session();
    CHECK(reboot->engine.epoch!=before && !reboot->engine.occupancy());
    reboot->pins(1);for(const auto& layer:reboot->engine.scene.layers)CHECK(!layer.visible);
    CHECK(animator.set_active(Character::Angel,12365));CHECK(animator.next_blink()>=15365);
    reboot->engine.tick(0);RenderLease lease;CHECK(reboot->engine.snapshot(lease));CHECK(lease.epoch==reboot->engine.epoch);
    CHECK(lease.scene.layers[0].image==&reboot->cache.find(angel.faceplate.hash)->handle);
    CHECK(lease.scene.layers[1].image==&reboot->cache.find(angel.eyes[angel.animator.open].hash)->handle);
    CHECK(lease.scene.layers[2].image==&reboot->cache.find(angel.mouths[0].hash)->handle);
    CHECK(lease.scene.layers[1].x==angel.eyes[angel.animator.open].x);
    for(unsigned n=3;n<8;++n)CHECK(!lease.scene.layers[n].visible);
    CHECK(reboot->engine.current(lease));reboot->engine.complete(lease,true,360*360);
    // Same zero-wait bridge as firmware: the display owner's mutex is busy
    // precisely when an idle feature update needs admission. No scene, pin,
    // sequence or fault changes until that lock becomes available.
    const auto due=animator.next_blink();const auto accepted=reboot->engine.accepted;
    const auto errors=reboot->engine.errors;const auto sequence=reboot->sequence;const auto unlocks=reboot->unlocks;
    reboot->lock_available=false;CHECK(!animator.tick(due,0,false,0));
    CHECK(reboot->admission.admission==DuoAdmission::Busy && reboot->admission.error==Error::None);
    CHECK(reboot->sequence==sequence && reboot->unlocks==unlocks && reboot->engine.accepted==accepted && reboot->engine.errors==errors);
    const auto intended=reboot->attempted;CHECK(intended.count==1);
    CHECK(!animator.tick(due+400,0,false,0));
    CHECK(!std::strcmp(reboot->attempted.operations[0].hash,intended.operations[0].hash));
    reboot->lock_available=true;CHECK(animator.tick(due+420,0,false,0));
    CHECK(reboot->admission.admission==DuoAdmission::Accepted && reboot->unlocks==unlocks+1);
    reboot->engine.tick(0);CHECK(reboot->engine.snapshot(lease));
    CHECK(lease.scene.layers[1].image==&reboot->cache.find(angel.eyes[angel.animator.half].hash)->handle);
    CHECK(lease.scene.layers[1].x==angel.eyes[angel.animator.half].x && lease.scene.layers[1].y==angel.eyes[angel.animator.half].y);
    std::vector<uint8_t> actual(360*360*2),previous(actual.size());std::array<Pixel,360> pixels{};
    CHECK(compose(lease.scene,{0,0,360,360},actual.data(),actual.size(),pixels.data(),pixels.size()));
    auto old_scene=lease.scene;old_scene.layers[1].image=&reboot->cache.find(angel.eyes[angel.animator.open].hash)->handle;
    CHECK(compose(old_scene,{0,0,360,360},previous.data(),previous.size(),pixels.data(),pixels.size()));
    CHECK(actual!=previous); // Real DGI decoder/compositor shows the deferred half-eye frame.
    CHECK(reboot->engine.current(lease));reboot->engine.complete(lease,true,360*360);
    CHECK(reboot->engine.completed_revision==lease.scene.revision && reboot->engine.errors==errors);
    const auto half_admitted=reboot->engine.accepted;
    CHECK(animator.tick(due+440,0,false,0) && animator.eye_frame()==angel.animator.half);
    CHECK(animator.tick(due+479,0,false,0) && animator.eye_frame()==angel.animator.half);
    CHECK(reboot->engine.accepted==half_admitted); // Not superseded inside the 33 ms render period.
    CHECK(animator.tick(due+480,0,false,0) && animator.eye_frame()==angel.animator.blink);
    // Real malformed/not-loaded engine rejections remain errors, never Busy.
    Request missing;missing.count=1;missing.operations[0].kind=Operation::Show;
    std::memset(missing.operations[0].hash,'a',64);CHECK(!reboot->cache.find(missing.operations[0].hash));
    CHECK(!reboot->scene(missing) && reboot->admission.admission==DuoAdmission::Rejected && reboot->admission.error==Error::NotLoaded);
    // Exhaust actual result slots with applied but unrendered fills. This is
    // transient queue backpressure; freeing a render lets the same batch in.
    Cache empty;FakeClock empty_clock;Engine saturated(empty,empty_clock);uint64_t number=0;
    Request fill;fill.count=1;fill.operations[0].kind=Operation::Fill;
    auto admit=[&]{return duo_admit_batch(fill,nullptr,saturated,number,[]{return true;},[]{},[]{return int64_t(0);});};
    for(unsigned n=0;n<31;++n){CHECK(admit().admission==DuoAdmission::Accepted);saturated.tick(0);}
    CHECK(admit().admission==DuoAdmission::Busy && saturated.dropped==1);
    CHECK(saturated.snapshot(lease));saturated.complete(lease,true,1);
    CHECK(admit().admission==DuoAdmission::Accepted);
    reboot->engine.end_session();reboot->pins(1);
    for(size_t i=0;i<count;++i)reboot->unpin(assets[i]);
    reboot->pins(0);
    const auto fetches=reboot->fetches;result=install(*reboot,assets,count,"");
    CHECK(result.error==Error::None && result.restored==31 && !result.fetched && reboot->fetches==fetches);reboot->pins(1);
    reboot.reset();std::filesystem::remove_all(root);
    std::puts("real store/cache/engine: lock/queue busy, exact deferred idle frame rendered, real rejection, install and epoch passed");
}
