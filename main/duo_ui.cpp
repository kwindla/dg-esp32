#include "duo_ui.h"
#include "duo_settings.h"
#include "dg_display/duo_runtime.h"
#include "dg_audio/character_signals.h"
#include "esp_heap_caps.h"
#include "esp_random.h"
#include <nlohmann/json.hpp>
#include <cstring>
#include <cstdio>
bool app_console_line(const char*)noexcept;
namespace dg {
using namespace dg_duo;
DuoUi::DuoUi():device((uint64_t(esp_random())<<32)|esp_random(),duo_last_character(),
    [](void* p,const ClientMessage& m){return static_cast<Mailbox*>(p)->post(m);},&outbound),
    animator_([](void* p,const dg_display::Request& q){auto& ui=*static_cast<DuoUi*>(p);
        ui.admission_=dg_display::duo_scene_admit(q,ui.swapping_?&ui.ticket_:nullptr);
        if(ui.admission_.admission==dg_display::DuoAdmission::Accepted){ui.busy_since_=-1;ui.busy_faulted_=false;}
        return ui.admission_.admission==dg_display::DuoAdmission::Accepted;},this){}
void DuoUi::lifecycle(bool live,uint32_t generation,int64_t now){
    live_=live;scene_due_=animated_=render_pending_=false;
    busy_since_=-1;busy_faulted_=false;
    if(live)device.connect(generation,now);else device.disconnect(generation);
}
bool DuoUi::receive(const char* text,bool truncated,uint32_t generation,int64_t now){
    try{
        const auto j=nlohmann::json::parse(text,nullptr,false);
        if(!j.is_object() || j.value("type",nlohmann::json())!="duo")return false;
        auto malformed=[&]{device.error(Fault::Malformed);return true;};
        if(truncated || (j.size()!=8 && !(j.size()==9 && j.contains("reason"))) || !j.contains("v") || !j["v"].is_number_unsigned() || j["v"]!=1)return malformed();
        Outcome o;
        auto copy=[&](const char* key,char* out,size_t size){
            if(!j.contains(key) || !j[key].is_string())return false;
            const auto& s=j[key].get_ref<const std::string&>();
            if(s.empty() || s.size()>=size || s.find('\0')!=std::string::npos)return false;
            // IDs are echoed into fixed JSON evidence without escaping.
            for(char c:s)if(!((c>='a' && c<='z') || (c>='A' && c<='Z') || (c>='0' && c<='9') || c=='-' || c=='_' || c=='.' || c==':'))return false;
            std::memcpy(out,s.c_str(),s.size()+1);return true;
        };
        if(!copy("session",o.session,sizeof(o.session)) || !copy("conn",o.conn,sizeof(o.conn)) ||
           !j.contains("seq") || !j["seq"].is_number_unsigned() || j["seq"]==0 || j["seq"].get<uint64_t>()>UINT32_MAX ||
           !j.contains("rev") || !j["rev"].is_number_unsigned())return malformed();
        o.seq=j["seq"].get<uint32_t>();o.rev=j["rev"].get<uint64_t>();
        if(j.value("op",nlohmann::json())=="state")o.state=true;
        else if(j.value("op",nlohmann::json())=="error")o.state=false;else return malformed();
        if(j.value("character",nlohmann::json())=="angel")o.character=Persona::Angel;
        else if(j.value("character",nlohmann::json())=="devil")o.character=Persona::Devil;else return malformed();
        if(j.contains("reason") && !copy("reason",o.reason,sizeof(o.reason)))return malformed();
        if(device.receive(o,generation,now)){
            proof_=o;render_pending_=false;
            scene_due_=true;animated_=false;duo_character_ack(o.character);
        }
        return true;
    }catch(...){device.error(Fault::Malformed);return true;}
}
void DuoUi::tap(bool injected,int64_t now){
    if(!accepts_touch())return;
    if(injected)++device.counts.taps_injected;else ++device.counts.taps_physical;
    device.tap(now);
}
void DuoUi::tick(int64_t now){
    if(now>=next_status_){next_status_=now+10000;status();}
    device.tick(now);
    if(!live_ || now<next_animation_)return;
    next_animation_=now+20;
    if(render_pending_){
        uint64_t revision=0;const auto result=dg_display::duo_scene_poll(ticket_,revision);
        if(result==dg_display::DuoCommit::Pending)return;
        render_pending_=false;
        if(result==dg_display::DuoCommit::Committed){++device.counts.displayed;proof(revision);animated_=true;}
        else{device.error(Fault::Render);animated_=false;}
    }
    if(scene_due_){
        swapping_=true;
        if(animator_.set_active(device.character()==Persona::Angel?Character::Angel:Character::Devil,now)){
            scene_due_=false;render_pending_=true;
        }else admission_failed("swap",now);
        swapping_=false;
    }else if(animated_){
        const auto audio=dg_audio::character_signals.read(uint32_t(now));
        if(!animator_.tick(now,audio.speaker_power,audio.mic_open,audio.mic_power))admission_failed("animation",now);
    }
}
void DuoUi::admission_failed(const char* phase,int64_t now){
    const bool busy=admission_.admission==dg_display::DuoAdmission::Busy;
    const char* reason=busy?(admission_.error==dg_display::Error::QueueFull?"queue-full":"lock-busy"):"rejected";
    if(busy){
        ++device.counts.render_busy;
        if(busy_since_<0)busy_since_=now;
        if(!busy_faulted_ && now-busy_since_>500){
            busy_faulted_=true;device.error(Fault::Render);reason="busy-timeout";
        }
    }else device.error(Fault::Render);
    char record[240];const int n=std::snprintf(record,sizeof(record),
        "\nDUO_RENDER {\"generation\":%u,\"rev\":%llu,\"at_ms\":%lld,\"phase\":\"%s\",\"reason\":\"%s\",\"error\":%u}\n",
        unsigned(device.generation()),(unsigned long long)device.revision(),(long long)now,phase,reason,unsigned(admission_.error));
    if(n>0 && size_t(n)<sizeof(record))app_console_line(record);
}
void DuoUi::proof(uint64_t revision){
    const auto& pack=proof_.character==Persona::Angel?angel:devil;
    char record[DuoRecordLimit+1];
    int n=std::snprintf(record,sizeof(record),
        "\nDUO_PROOF {\"session\":\"%s\",\"generation\":%u,\"conn\":\"%s\",\"seq\":%u,\"rev\":%llu,\"character\":\"%s\",\"pack\":\"%s\",\"scene_revision\":%llu,\"layer_count\":%u,\"hidden\":[",
        device.session(),unsigned(device.generation()),proof_.conn,unsigned(proof_.seq),(unsigned long long)proof_.rev,name(proof_.character),pack.hash,(unsigned long long)revision,pack.layer_count);
    for(unsigned layer=pack.layer_count;layer<8;++layer){
        if(n<0 || size_t(n)>=sizeof(record))return;
        n+=std::snprintf(record+n,sizeof(record)-n,"%s%u",layer==pack.layer_count?"":",",layer);
    }
    if(n<0 || size_t(n)>=sizeof(record))return;
    n+=std::snprintf(record+n,sizeof(record)-n,"]}\n");
    if(n<0 || size_t(n)>=sizeof(record))return;
    app_console_line(record);
    for(unsigned layer=0;layer<pack.layer_count;++layer){
        const Frame* frame=nullptr;
        switch(pack.order[layer]){case Layer::Faceplate:frame=&pack.faceplate;break;
            case Layer::Eyes:frame=&pack.eyes[pack.animator.open];break;
            case Layer::Mouth:frame=&pack.mouths[pack.animator.mouth[0]];break;}
        n=std::snprintf(record,sizeof(record),
            "\nDUO_PROOF_LAYER {\"rev\":%llu,\"scene_revision\":%llu,\"index\":%u,\"hash\":\"%s\",\"x\":%d,\"y\":%d}\n",
            (unsigned long long)proof_.rev,(unsigned long long)revision,layer,frame->hash,frame->x,frame->y);
        if(n<0 || size_t(n)>=sizeof(record))return;
        app_console_line(record);
    }
}
void DuoUi::status(){
    const auto& c=device.counts;char record[DuoRecordLimit+1];
    int n=std::snprintf(record,sizeof(record),
        "\nDUO_STATUS {\"generation\":%u,\"toggles_sent\":%u,\"retransmitted\":%u,\"resyncs\":%u,\"answered\":%u,\"displayed\":%u,\"render_busy\":%u,\"taps_ignored_pending\":%u,\"taps_injected\":%u,\"taps_physical\":%u,\"character\":\"%s\",\"rev\":%llu,\"baseline\":%s,\"pending\":%s,\"internal_heap_free\":%u,\"internal_heap_minimum\":%u}\n",
        unsigned(device.generation()),unsigned(c.toggles),unsigned(c.retransmitted),unsigned(c.resyncs),unsigned(c.answered),unsigned(c.displayed),unsigned(c.render_busy),unsigned(c.taps_ignored),unsigned(c.taps_injected),unsigned(c.taps_physical),
        name(device.character()),(unsigned long long)device.revision(),device.baseline()?"true":"false",device.pending()?"true":"false",
        unsigned(heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT)),
        unsigned(heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT)));
    if(n<0 || size_t(n)>=sizeof(record))return;
    app_console_line(record);
    // Two bounded error fragments, joined only within this status sample.
    constexpr unsigned per_part=8,parts=(unsigned(Fault::Count)+per_part-1)/per_part;
    for(unsigned part=0;part<parts;++part){
        n=std::snprintf(record,sizeof(record),
            "\nDUO_ERRORS {\"generation\":%u,\"rev\":%llu,\"part\":%u,\"parts\":%u,\"errors\":{",
            unsigned(device.generation()),(unsigned long long)device.revision(),part,parts);
        for(unsigned f=part*per_part;f<unsigned(Fault::Count) && f<(part+1)*per_part;++f){
            if(n<0 || size_t(n)>=sizeof(record))return;
            n+=std::snprintf(record+n,sizeof(record)-n,"%s\"%s\":%u",f%per_part?",":"",fault_name(Fault(f)),unsigned(c.errors[f]));
        }
        if(n<0 || size_t(n)>=sizeof(record))return;
        n+=std::snprintf(record+n,sizeof(record)-n,"}}\n");
        if(n<0 || size_t(n)>=sizeof(record))return;
        app_console_line(record);
    }
}

}
