#include "../../src/runtime_state.h"
namespace dg_display {namespace detail {
void release_demo(){state->demo_enabled=false;for(auto*& e:state->demo_pins)if(e){state->cache.unpin(*e,Pin::Scheduled);e=nullptr;}}
void start_character(){
    for(unsigned i=0;i<16;++i){auto* e=state->cache.find(state->assets[i]);if(!e || !state->cache.pin(*e,Pin::Scheduled)){release_bench();throw std::runtime_error("character asset not-loaded");}state->bench_pins[i]=e;}
    state->character.reset(esp_timer_get_time()/1000);state->character_frame={99,99,99};state->character_next=0;
    for(auto& count:state->character_changes)count=0;
    state->character_posts=state->character_errors=0;state->character_enabled=true;
}
bool demo_tick(int64_t now){
    if(!state->demo_enabled)return true;
    const auto frame=state->demo_animation.tick(requested_screen.load(),now/1000);
    const bool changed=!state->demo_started || frame.screen!=state->demo_frame.screen;
    if(frame.screen==DemoScreen::Live){
        if(changed){state->character.reset(now/1000);state->character_frame={99,99,99};state->character_next=0;}
        state->demo_frame=frame;state->demo_started=true;return true;
    }
    const unsigned base_asset=frame.screen==DemoScreen::Disconnected?0:1;
    const bool base=changed || !state->engine.scene.layers[0].visible || state->engine.scene.layers[0].image!=&state->demo_pins[base_asset]->handle;
    const bool dots=frame.screen==DemoScreen::Connecting && (base || frame.dots!=state->demo_frame.dots);
    if(!base && !dots)return false;
    auto& q=state->bench_request;q.~Request();new(&q)Request{};meta(q,"demo");
    q.meta.sequence=++state->bench_sequence;std::snprintf(q.meta.id,33,"demo-%llu",(unsigned long long)q.meta.sequence);
    auto show=[&](unsigned layer,unsigned asset){auto& op=q.operations[q.count++];op.kind=Operation::Show;op.layer=layer;op.x=demo_assets[asset].x;op.y=demo_assets[asset].y;std::strcpy(op.hash,demo_assets[asset].hash);};
    if(base){show(0,base_asset);for(unsigned layer=1;layer<8;++layer){
        if(layer==1 && dots)continue;
        auto& op=q.operations[q.count++];op.kind=Operation::Hide;op.layer=layer;
    }}
    if(dots)show(1,2+frame.dots);
    const auto admitted=state->engine.post(q,now,true);
    if(admitted.admission!=Admission::Accepted){++state->demo_errors;state->failed=true;return false;}
    ++state->demo_posts;state->demo_frame=frame;state->demo_started=true;return false;
}
void character_tick(int64_t now){
    if(!state->character_enabled || now<state->character_next)return;
    state->character_next=now+20000;
    state->character_audio=dg_audio::character_signals.read(uint32_t(now/1000));
    const auto audio=state->character_audio;
    const auto frame=state->character.tick(now/1000,audio.speaker_power,audio.mic_open,audio.mic_power);
    const bool base=!state->engine.scene.layers[0].visible || state->engine.scene.layers[0].image!=&state->bench_pins[0]->handle;
    const bool changed[]={base || frame.mouth!=state->character_frame.mouth,
                          base || frame.eyes!=state->character_frame.eyes,
                          base || frame.whiskers!=state->character_frame.whiskers};
    if(!base && !changed[0] && !changed[1] && !changed[2])return;
    auto& q=state->bench_request;q.~Request();new(&q)Request{};meta(q,"character");
    q.meta.sequence=++state->bench_sequence;std::snprintf(q.meta.id,33,"character-%llu",(unsigned long long)q.meta.sequence);
    auto show=[&](unsigned layer,unsigned asset,int x,int y){auto& op=q.operations[q.count++];op.kind=Operation::Show;op.layer=layer;op.x=x;op.y=y;std::strcpy(op.hash,state->assets[asset]);};
    if(base){show(0,0,0,0);for(unsigned layer=4;layer<8;++layer){auto& op=q.operations[q.count++];op.kind=Operation::Hide;op.layer=layer;}}
    if(changed[0])show(1,1+frame.mouth,cat_coordinate(168),cat_coordinate(192));
    if(changed[1])show(2,7+frame.eyes,cat_coordinate(152),cat_coordinate(176));
    if(changed[2])show(3,13+frame.whiskers,cat_coordinate(92),cat_coordinate(180));
    // Scene dirty rectangles recompose the base and every intersecting alpha
    // layer. Only changed feature patches are sent after the initial face.
    const auto admitted=state->engine.post(q,now,true);
    if(admitted.admission!=Admission::Accepted){++state->character_errors;state->failed=true;state->character_enabled=false;release_bench();return;}
    ++state->character_posts;for(unsigned i=0;i<3;++i)state->character_changes[i]+=changed[i];
    state->character_frame=frame;
}
void demo_rendered(const RenderLease& lease,int64_t now){
    if(!state->demo_enabled)return;
    const auto screen=demo_scene_screen(lease.scene,
        [&](unsigned i){auto* pin=state->bench_pins[i];return pin?&pin->handle:nullptr;},
        [&](unsigned i){auto* pin=state->demo_pins[i];return pin?&pin->handle:nullptr;});
    if(screen==99 || screen==state->demo_last_proof)return;
    state->demo_last_proof=screen;
    const auto number=++state->demo_proof_count;
    state->demo_proofs[(number-1)%16]={number,lease.scene.revision,state->engine.rendered,now,screen};
    dg_deferred_log("DISPLAY_DEMO_RENDERED {\"n\":%llu,\"screen\":%u,\"completed_us\":%lld,\"revision\":%llu,\"rendered\":%llu}",(unsigned long long)number,screen,(long long)now,(unsigned long long)lease.scene.revision,(unsigned long long)state->engine.rendered);
}
}}
