#include "dg_display/panel_config.h"
#include "dg_display/engine.h"
#include <cstring>
#include <algorithm>
#include <new>
namespace dg_display {
namespace {
template<class T>void reset(T& value){value.~T();new(&value) T{};}
bool identifier(const char* s,size_t capacity,bool required=true){size_t n=0;while(n<capacity && s[n]){char c=s[n++];if(!((c>='a' && c<='z') || (c>='A' && c<='Z') || (c>='0' && c<='9') || c=='-' || c=='_' || c=='.' || c==':'))return false;}return n<capacity && (!required || n);}
bool text(const char* s,size_t capacity,bool required=true) {size_t n=0;while(n<capacity && s[n]){unsigned c=unsigned(static_cast<unsigned char>(s[n]));if(c<33 || c>126)return false;++n;}return n<capacity && (!required || n);}
bool same(const Request& a,const Request& b) {
    if(a.kind!=b.kind || a.meta.version!=b.meta.version || a.meta.epoch!=b.meta.epoch || a.meta.sequence!=b.meta.sequence || std::strcmp(a.meta.id,b.meta.id) || a.count!=b.count || a.time!=b.time || a.timestamp!=b.timestamp || a.clock_epoch!=b.clock_epoch || std::strcmp(a.utterance,b.utterance) || std::strcmp(a.hash,b.hash) || std::strcmp(a.url,b.url) || a.bytes!=b.bytes || a.any_host!=b.any_host)return false;
    for(unsigned i=0;i<a.count;++i){auto& x=a.operations[i];auto& y=b.operations[i];if(x.kind!=y.kind || x.layer!=y.layer || x.x!=y.x || x.y!=y.y || x.color!=y.color || std::strcmp(x.hash,y.hash))return false;}return true;
}
uint8_t mask(const Request& r){uint8_t n=0;for(unsigned i=0;i<r.count;++i)n|=r.operations[i].kind==Operation::Fill?255:uint8_t(1u<<r.operations[i].layer);return n;}
bool retain(Layer l,Pin pin){return !l.visible || !l.image || !l.image->retain || l.image->retain(l.image->reference,pin);}
void release_handle(Layer l,Pin pin){if(l.visible && l.image && l.image->release)l.image->release(l.image->reference,pin);}
}
Result* Engine::result(const char* id) noexcept {for(auto& r:results)if(r.state!=ResultState::Empty && !std::strcmp(r.request.meta.id,id))return &r;return nullptr;}
bool Engine::cancelled(const char* u)const noexcept {if(!u[0])return false;for(auto& t:tombstones)if(!std::strcmp(t,u))return true;return false;}
unsigned Engine::occupancy()const noexcept {unsigned n=0;for(auto& p:pending)n+=p.occupied;return n;}
void Engine::release(Pending& p) noexcept {for(auto* e:p.reservations)if(e)cache.unpin(*e,Pin::Scheduled);reset(p);}
void Engine::terminal(Result& r,Error e,bool persisted) noexcept {if(r.state!=ResultState::Pending)return;r.error=e;r.persisted=persisted;r.state=e==Error::None?ResultState::Completed:e==Error::Cancelled?ResultState::Cancelled:e==Error::Expired?ResultState::Expired:ResultState::Rejected;if(e!=Error::None)++errors;}
void Engine::cancel_pending(const char* u,uint8_t affected,bool all) noexcept {
    for(auto& p:pending)if(p.occupied && (all || (u && !std::strcmp(p.request.utterance,u)) || (affected && (mask(p.request)&affected)))){
        auto& r=results[p.result];r.state=u || all?ResultState::Cancelled:ResultState::Superseded;r.error=u || all?Error::Cancelled:Error::None;if(r.state==ResultState::Superseded){++superseded;++coalesced;}release(p);
    }
}
PostResult Engine::post(const Request& q,int64_t now,bool internal) noexcept {
    ++requested;
    auto invalid=[&](Error e){++errors;return PostResult{Admission::Invalid,e,false};};
    if(q.meta.version!=1)return invalid(Error::Version);
    if(q.meta.epoch!=epoch)return invalid(Error::StaleEpoch);
    if(!identifier(q.meta.id,sizeof(q.meta.id)) || !q.meta.sequence || !identifier(q.utterance,sizeof(q.utterance),false) || unsigned(q.kind)>unsigned(RequestKind::Cancel) || unsigned(q.time)>unsigned(TimeKind::Arrival) || q.count>8)return invalid(Error::Malformed);
    // Validate all fixed strings before comparing a replay (no unterminated input is ever read).
    if(!text(q.hash,sizeof(q.hash),false) || !text(q.url,sizeof(q.url),false))return invalid(Error::Malformed);
    for(unsigned i=0;i<q.count;++i)if(!text(q.operations[i].hash,sizeof(q.operations[i].hash),false))return invalid(Error::Malformed);
    if(auto* old=result(q.meta.id)){if(!same(old->request,q))return invalid(Error::IdReuse);return {Admission::Accepted,old->error,true};}
    if(!internal && q.meta.sequence<=last_sequence)return invalid(Error::StaleSequence);
    unsigned slot=31;
    if(q.kind!=RequestKind::Cancel){slot=32;for(unsigned i=0;i<31;++i)if(results[i].state==ResultState::Empty){slot=i;break;}if(slot==32)for(unsigned i=0;i<31;++i)if(results[i].state!=ResultState::Pending && results[i].state!=ResultState::Applied && (slot==32 || results[i].order<results[slot].order))slot=i;}
    if(slot==32){++dropped;return {Admission::Full,Error::QueueFull,false};}
    auto& item=admission_scratch;reset(item);unsigned pending_slot=16;
    if(q.kind==RequestKind::Scene){
        if(!q.count || cancelled(q.utterance))return invalid(cancelled(q.utterance)?Error::Cancelled:Error::Malformed);
        if(q.time!=TimeKind::Immediate){auto p=clock.map(q.timestamp,q.clock_epoch);if(q.time!=TimeKind::Rtp || !q.utterance[0] || p.state!=MappingState::Valid || p.uncertainty_us>50000 || p.speaker_us-render_latency_us-visual_lead_us<now-Late || p.speaker_us-render_latency_us-visual_lead_us>now+Horizon)return invalid(Error::Expired);}
        for(unsigned i=0;i<q.count;++i){const auto& op=q.operations[i];if(unsigned(op.kind)>unsigned(Operation::Fill) || op.layer>=8){release(item);return invalid(Error::Malformed);}if(op.kind==Operation::Show){if(!valid_hash(op.hash)){release(item);return invalid(Error::Malformed);}auto* e=cache.find(op.hash);if(!e || !cache.pin(*e,Pin::Scheduled)){release(item);return invalid(Error::NotLoaded);}item.reservations[i]=e;}}
        // Immediate replacement can reuse the slot of the WHOLE superseded batch.
        for(unsigned i=0;i<16;++i)if(!pending[i].occupied || (q.time==TimeKind::Immediate && (mask(q)&mask(pending[i].request)))){pending_slot=i;break;}
        if(pending_slot==16){release(item);++dropped;return {Admission::Full,Error::QueueFull,false};}
    }else if(q.kind!=RequestKind::Cancel){
        if(!valid_hash(q.hash) || q.count || q.time!=TimeKind::Immediate)return invalid(Error::Malformed);
        if(q.kind==RequestKind::Load && (!q.bytes || q.bytes>524288 || std::strncmp(q.url,"http://",7)))return invalid(q.bytes>524288?Error::RamBudget:Error::Malformed);
        if(job_count==2){++dropped;return {Admission::Full,Error::QueueFull,false};}
    }else if(!q.utterance[0] || q.count || q.time!=TimeKind::Immediate)return invalid(Error::Malformed);
    reset(results[slot]);auto& r=results[slot];r.request=q;r.state=ResultState::Pending;r.order=++result_order;
    if(!internal)last_sequence=q.meta.sequence;
    ++accepted;
    if(q.kind==RequestKind::Cancel){
        std::memcpy(tombstones[tombstone_next++%16],q.utterance,33);cancel_pending(q.utterance,0,false);
        if(rendering && !std::strcmp(rendering_scene.utterance,q.utterance)){++generation;dirty.add({0,0,PanelWidth,PanelHeight});}
        terminal(r,Error::None);
    }else if(q.kind==RequestKind::Scene){
        if(q.time==TimeKind::Immediate)cancel_pending(nullptr,mask(q),false);
        item.request=q;item.occupied=true;item.result=slot;pending[pending_slot]=item;
    }else jobs[job_count++]=slot;
    return {Admission::Accepted,Error::None,false};
}
void Engine::apply(Pending& p) noexcept {
    for(unsigned i=0;i<p.request.count;++i){auto& op=p.request.operations[i];if(op.kind==Operation::Fill){scene.background=op.color;dirty.add({0,0,PanelWidth,PanelHeight});continue;}
        auto& layer=scene.layers[op.layer];dirty.add(bounds(layer));release_handle(layer,Pin::Visible);
        layer=Layer{};if(op.kind==Operation::Show){auto* e=p.reservations[i];cache.pin(*e,Pin::Visible);layer={&e->handle,op.x,op.y,true};}dirty.add(bounds(layer));
    }
    std::memcpy(scene.utterance,p.request.utterance,33);scene.revision=++accepted_revision;auto& r=results[p.result];r.revision=scene.revision;r.state=ResultState::Applied;release(p);
}
void Engine::tick(int64_t now) noexcept {
    unsigned applied=0;
    for(unsigned pass=0;pass<16;++pass){int best=-1;int64_t best_due=INT64_MAX;
        for(unsigned i=0;i<16;++i){auto& p=pending[i];if(!p.occupied)continue;int64_t due=now;
            if(p.request.time!=TimeKind::Immediate){auto mapped=clock.map(p.request.timestamp,p.request.clock_epoch);due=mapped.speaker_us-render_latency_us-visual_lead_us;
                if(mapped.state!=MappingState::Valid || mapped.uncertainty_us>50000 || now-due>Late){terminal(results[p.result],Error::Expired);release(p);continue;}}
            if(due<=now && (best<0 || due<best_due || (due==best_due && p.request.meta.sequence<pending[best].request.meta.sequence))){best=int(i);best_due=due;}}
        if(best<0)break;
        if(now-best_due>33333)++deadline_misses;
        apply(pending[best]);++applied;
    }
    if(applied>1)coalesced+=applied-1;
    if(applied && !dirty.count && !rendering){completed_revision=scene.revision;for(auto& r:results)if(r.state==ResultState::Applied && r.revision<=completed_revision)r.state=r.revision==completed_revision?ResultState::Completed:ResultState::Superseded;}
}
bool Engine::snapshot(RenderLease& lease) noexcept {
    if(rendering || !dirty.count)return false;
    for(unsigned i=0;i<8;++i)if(!retain(scene.layers[i],Pin::Rendering)){for(unsigned j=0;j<i;++j)release_handle(scene.layers[j],Pin::Rendering);return false;}
    lease={scene,dirty,epoch,generation,true};dirty.clear();rendering=true;rendering_scene=scene;rendering_revision=scene.revision;
    return true;
}
void Engine::complete(RenderLease& lease,bool success,uint64_t pushed) noexcept {
    if(!lease.valid)return;
    for(auto& layer:lease.scene.layers)release_handle(layer,Pin::Rendering);
    pixels+=pushed;
    if(success && current(lease)){++rendered;completed_revision=lease.scene.revision;for(auto& r:results)if(r.state==ResultState::Applied && r.revision<=completed_revision)r.state=r.revision==completed_revision?ResultState::Completed:ResultState::Superseded;}
    else dirty.add({0,0,PanelWidth,PanelHeight});
    lease.valid=false;rendering=false;
}
Result* Engine::next_job() noexcept {if(!job_count)return nullptr;unsigned index=jobs[0];for(unsigned i=1;i<job_count;++i)jobs[i-1]=jobs[i];--job_count;return &results[index];}
void Engine::reset_scene() noexcept {for(auto& l:scene.layers)release_handle(l,Pin::Visible);uint16_t background=scene.background;scene=Scene{};scene.background=background;scene.revision=++accepted_revision;dirty.add({0,0,PanelWidth,PanelHeight});++generation;}
void Engine::transport_down() noexcept {cancel_pending(nullptr,0,true);for(auto& r:results)if(r.state==ResultState::Pending)terminal(r,Error::Cancelled);job_count=0;reset_scene();}
void Engine::start_session() noexcept {transport_down();++epoch;last_sequence=0;for(auto& r:results)if(r.state!=ResultState::Pending)reset(r);for(auto& t:tombstones)t[0]=0;session=true;}
void Engine::end_session() noexcept {transport_down();session=false;/* Session pins survive until durable persistence or explicit delete. */}
}
