// Included only by runtime.cpp under CONFIG_DG_DUO, to share its guarded store.
#include <string>
namespace dg_display {
namespace {
Error duo_wait(Request& q,bool durable=true) {
    {Guard guard;if(state->session_active)return Error::InUse;
     q.meta.epoch=state->engine.epoch;q.meta.sequence=state->engine.last_sequence+1;
     std::snprintf(q.meta.id,33,"duo-store-%u",++state->console_sequence);}
    auto admitted=post(q);if(admitted.admission!=Admission::Accepted)return admitted.error;
    const auto deadline=esp_timer_get_time()+35000000;
    while(esp_timer_get_time()<deadline){
        {Guard guard;auto* r=state->engine.result(q.meta.id);if(!r)return Error::Expired;
         if(r->state!=ResultState::Pending && r->state!=ResultState::Applied){
             if(r->error!=Error::None)return r->error;
             return r->state==ResultState::Completed && (!durable || r->persisted)?Error::None:Error::StorageUnavailable;
         }}vTaskDelay(pdMS_TO_TICKS(10)+1);
    }
    return Error::Expired;
}
}
// The session worker closes idle admission under the same gate that covers
// NVS writes. A writer already inside the gate finishes before the session.
bool duo_session_start(){
    if(!state || state->dead || xSemaphoreTake(state->gate,pdMS_TO_TICKS(2000))!=pdTRUE)return false;
    state->session_active=true;
    configASSERT(xSemaphoreGive(state->gate)==pdTRUE);
    session_start();return true;
}
Error duo_prepare(){
    if(!state)census_init(true,20480);
    runtime_wait_ready();
    {Guard guard;if(state->session_active)return Error::InUse;if(!state->fs_ready)return Error::StorageUnavailable;
     state->defer_persistence=true;}
    bool ready;{Guard guard;ready=state->cache.arena.budget()!=0;}
    if(!ready)allocate_self_arena();
    return Error::None;
}
Error duo_inspect(const DuoAsset& a){
    Guard guard;if(!state->fs_ready)return Error::StorageUnavailable;
    for(const auto& i:state->inventory)if(!std::strcmp(i.hash,a.hash)){
        if(!i.valid)return i.error;
        return i.bytes==a.bytes?Error::None:Error::Hash;
    }return Error::NotLoaded;
}
Error duo_admit(const DuoAsset* assets,size_t count){
    Guard guard;if(state->session_active)return Error::InUse;
    DuoStoreRoom room;room.inventory=state->inventory;room.inventory_slots=sizeof(state->inventory)/sizeof(state->inventory[0]);
    room.catalog_files=state->fs_count;room.flash_used=state->fs_used;room.flash_temporary=state->fs_temp;
    room.flash_capacity=state->fs_capacity;room.flash_free=state->fs_free;room.flash_admissible=state->fs_admissible;
    room.arena_budget=state->cache.arena.budget();
    return duo_store_admission(assets,count,room);
}
Error duo_restore(const DuoAsset& a){Request q{};q.kind=RequestKind::Preload;std::strcpy(q.hash,a.hash);return duo_wait(q);}
Error duo_fetch(const DuoAsset& a,const char* origin){
    std::string setting="origin ";setting+=origin;command(setting.c_str(),true,[](const char*){});
    Request q{};q.kind=RequestKind::Load;q.bytes=a.bytes;std::strcpy(q.hash,a.hash);
    const auto n=std::snprintf(q.url,sizeof(q.url),"%s/%s.dgi",origin,a.hash);
    if(n<0 || size_t(n)>=sizeof(q.url))return Error::Malformed;
    auto error=duo_wait(q,false);if(error!=Error::None)return error;
    // Explicit persist returns the precise failure if auto-persist failed.
    q=Request{};q.kind=RequestKind::Persist;std::strcpy(q.hash,a.hash);error=duo_wait(q);
    if(error!=Error::None){Guard guard;if(auto* e=state->cache.find(a.hash))state->cache.unpin(*e,Pin::Session);}
    return error;
}
Error duo_pin(const DuoAsset& a){Guard guard;auto* e=state->cache.find(a.hash);
    if(!e || !e->persisted || e->size!=a.bytes)return Error::NotLoaded;
    if(state->cache.arena.used()>state->cache.arena.budget()*3/4)return Error::RamBudget;
    return state->cache.pin(*e,Pin::Scheduled)?Error::None:Error::InUse;
}
void duo_unpin(const DuoAsset& a)noexcept{Guard guard(false);if(!guard)return;
    if(auto* e=state->cache.find(a.hash))state->cache.unpin(*e,Pin::Scheduled);
}
DuoMemory duo_memory(){DuoMemory m;if(!state)return m;Guard guard;
    m.used=state->cache.arena.used();m.largest=state->cache.arena.largest();
    for(const auto& e:state->cache.entries)if(e.valid && e.references())++m.pinned;
    return m;
}
bool duo_status_screen(bool connecting){
    if(!state)return false;
    Guard guard(false);if(!guard)return false;
    const auto& a=demo_assets[connecting?1:0];
    auto* e=state->cache.find(a.hash);if(!e || !e->valid)return false;
    Request q{};meta(q,"duo-screen");q.meta.sequence=++state->bench_sequence;
    std::snprintf(q.meta.id,33,"duo-screen-%llu",(unsigned long long)state->bench_sequence);q.count=8;
    for(unsigned i=0;i<8;++i){auto& op=q.operations[i];op.layer=i;op.kind=i?Operation::Hide:Operation::Show;}
    std::strcpy(q.operations[0].hash,a.hash);
    return state->engine.post(q,esp_timer_get_time(),true).admission==Admission::Accepted;
}
bool duo_scene_post(const Request& batch,DuoTicket* ticket){
    return duo_scene_admit(batch,ticket).admission==DuoAdmission::Accepted;
}
DuoPostResult duo_scene_admit(const Request& batch,DuoTicket* ticket){
    if(!state || state->dead || state->failed || state->engine_lock_failed)return {DuoAdmission::Rejected,Error::StorageUnavailable};
    // The display owner also takes this mutex for tick/snapshot/completion.
    // A zero-wait miss is scheduling backpressure, not a rejected scene.
    return duo_admit_batch(batch,ticket,state->engine,state->bench_sequence,
        []{return xSemaphoreTake(state->lock,0)==pdTRUE;},
        []{xSemaphoreGive(state->lock);},[]{return esp_timer_get_time();});
}
DuoCommit duo_scene_poll(DuoTicket ticket,uint64_t& revision){
    if(!state || state->dead)return DuoCommit::Failed;
    if(xSemaphoreTake(state->lock,0)!=pdTRUE)return DuoCommit::Pending;
    struct Unlock {~Unlock(){xSemaphoreGive(state->lock);}} unlock;
    char id[33];std::snprintf(id,sizeof(id),"duo-scene-%llu",(unsigned long long)ticket.number);
    const auto* result=state->engine.result(id);
    const auto commit=state->duo_rendered.poll(result,ticket,state->engine.epoch);
    if(commit==DuoCommit::Committed)revision=result->revision;
    return commit;
}
}
