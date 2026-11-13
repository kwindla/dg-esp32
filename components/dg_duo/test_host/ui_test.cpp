#include "duo_ui.h"
#include "duo_settings.h"
#include "dg_display/duo_runtime.h"
#include "dg_audio/character_signals.h"
#include "esp_heap_caps.h"
#include <nlohmann/json.hpp>
#include "check.h"
#include <cstring>
#include <string>
#include <vector>
using namespace dg_duo;
namespace {
std::vector<std::string> lines;std::vector<dg_display::Request> batches;
bool accept=true,busy=false;dg_display::DuoCommit rendered=dg_display::DuoCommit::Pending;
dg_display::Error busy_error=dg_display::Error::None;
dg_display::Request attempted;unsigned attempts=0;
size_t heap_free=123456,heap_minimum=98765;
uint64_t scene_revision=123;
unsigned writes=0;Persona acknowledged=Persona::Angel;
std::string response(dg::DuoUi& ui,uint64_t rev=1,const char* character="angel",uint32_t seq=0){
    return nlohmann::json{{"type","duo"},{"v",1},{"op","state"},{"session",ui.device.session()},
        {"conn","epoch"},{"seq",seq?seq:ui.device.request().seq},{"rev",rev},{"character",character}}.dump();
}
unsigned proofs(){unsigned n=0;for(const auto& line:lines)n+=line.find("DUO_PROOF {")!=std::string::npos;return n;}
nlohmann::json latest(const char* prefix){
    const auto token=std::string(prefix)+" ";
    for(auto it=lines.rbegin();it!=lines.rend();++it)if(it->find(token)!=std::string::npos)
        return nlohmann::json::parse(it->substr(it->find('{')));
    CHECK(false);return {};
}
nlohmann::json proof_record(){
    auto j=latest("DUO_PROOF");j["layers"]=nlohmann::json::array();
    for(const auto& line:lines)if(line.find("DUO_PROOF_LAYER ")!=std::string::npos){
        auto p=nlohmann::json::parse(line.substr(line.find('{')));
        if(p["rev"]==j["rev"] && p["scene_revision"]==j["scene_revision"])
            j["layers"].push_back({p["index"],p["hash"],p["x"],p["y"]});
    }
    CHECK(j["layers"].size()==j["layer_count"]);return j;
}
nlohmann::json status_record(){
    auto j=latest("DUO_STATUS");j["errors"]=nlohmann::json::object();
    for(size_t i=lines.size()-2;i<lines.size();++i){
        auto p=nlohmann::json::parse(lines[i].substr(lines[i].find('{')));
        CHECK(p["generation"]==j["generation"] && p["rev"]==j["rev"] && p["part"]==i-(lines.size()-2) && p["parts"]==2);
        j["errors"].update(p["errors"]);
    }
    return j;
}
}
bool app_console_line(const char* s)noexcept{lines.emplace_back(s);return true;}
namespace dg {
Persona duo_last_character(){return Persona::Angel;}
void duo_character_ack(Persona c){acknowledged=c;++writes;}
}
size_t heap_caps_get_free_size(unsigned caps){CHECK(caps==(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT));return heap_free;}
size_t heap_caps_get_minimum_free_size(unsigned caps){CHECK(caps==(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT));return heap_minimum;}
namespace dg_display {
DuoPostResult duo_scene_admit(const Request& q,DuoTicket* ticket){
    attempted=q;++attempts;
    if(busy)return {DuoAdmission::Busy,busy_error};
    if(!accept)return {DuoAdmission::Rejected,Error::NotLoaded};
    batches.push_back(q);if(ticket)*ticket={1,batches.size()};return {DuoAdmission::Accepted,Error::None};
}
DuoCommit duo_scene_poll(DuoTicket,uint64_t& revision){revision=scene_revision;return rendered;}
}
int main(){
    dg::DuoUi ui;ui.lifecycle(true,1,0);ClientMessage sent;CHECK(ui.outbound.take(sent));
    CHECK(sent.seq==1 && sent.op==Op::Hello && sent.have==Persona::Angel);
    const auto first=response(ui);CHECK(ui.receive(first.c_str(),false,1,0));
    CHECK(writes==1);ui.tick(0);CHECK(batches.size()==1 && proofs()==0);
    ui.tick(20);CHECK(proofs()==0 && ui.device.counts.displayed==0);
    rendered=dg_display::DuoCommit::Committed;ui.tick(40);CHECK(proofs()==1 && ui.device.counts.displayed==1);
    auto j=proof_record();
    CHECK(j["session"]==ui.device.session() && j["conn"]=="epoch" && j["seq"]==1 && j["rev"]==1);
    CHECK(j["character"]=="angel" && j["pack"]==angel.hash && j["scene_revision"]==123);
    CHECK(j["layers"].size()==3 && j["layers"][1][1]==angel.eyes[angel.animator.open].hash);
    CHECK(j["layers"][1][2]==angel.eyes[angel.animator.open].x && j["layers"][1][3]==angel.eyes[angel.animator.open].y);
    CHECK(j["hidden"]==nlohmann::json({3,4,5,6,7}));
    auto count=batches.size();CHECK(ui.receive(first.c_str(),false,1,40));ui.tick(60);CHECK(batches.size()==count && proofs()==1);
    // Real gesture routes into the existing session; a second tap is counted.
    ui.tap(false,80);ui.tick(80);CHECK(ui.outbound.take(sent) && sent.op==Op::Toggle && sent.generation==1);
    ui.tap(false,100);ui.tick(100);CHECK(ui.device.counts.taps_ignored==1);
    const auto second=response(ui,2,"devil");CHECK(ui.receive(second.c_str(),false,1,100));
    accept=false;ui.tick(120);CHECK(proofs()==1 && acknowledged==Persona::Devil);
    accept=true;rendered=dg_display::DuoCommit::Pending;ui.tick(140);CHECK(batches.back().count==8);
    // New authoritative state supersedes an uncommitted swap, with no old proof.
    const auto third=response(ui,3,"angel",sent.seq);ui.receive(third.c_str(),false,1,140);ui.tick(160);
    rendered=dg_display::DuoCommit::Committed;ui.tick(180);CHECK(proofs()==2);
    j=proof_record();CHECK(j["rev"]==3 && j["character"]=="angel");
    // Failed render emits no proof. Generation changes abandon any pending proof.
    const auto fourth=response(ui,4,"devil");ui.receive(fourth.c_str(),false,1,180);ui.tick(200);
    rendered=dg_display::DuoCommit::Failed;ui.tick(220);CHECK(proofs()==2);
    const auto fifth=response(ui,5,"angel");ui.receive(fifth.c_str(),false,1,220);ui.tick(240);
    ui.lifecycle(false,2,250);rendered=dg_display::DuoCommit::Committed;ui.tick(260);CHECK(proofs()==2);
    // Strict shape/type parser and generation fence.
    ui.lifecycle(true,3,280);CHECK(ui.outbound.take(sent));const auto valid=response(ui,1,"angel");
    CHECK(ui.receive(valid.c_str(),false,1,280));CHECK(!ui.device.baseline());
    auto bad=nlohmann::json::parse(valid);bad["seq"]=true;CHECK(ui.receive(bad.dump().c_str(),false,3,280));CHECK(!ui.device.baseline());
    CHECK(ui.receive(valid.c_str(),true,3,280));CHECK(!ui.device.baseline());
    auto unknown=nlohmann::json::parse(valid);unknown["unexpected"]=1;
    CHECK(ui.receive(unknown.dump().c_str(),false,3,280));CHECK(!ui.device.baseline());
    auto adoption=nlohmann::json::parse(valid);adoption["reason"]="adoption-failed";
    CHECK(ui.receive(adoption.dump().c_str(),false,3,280));CHECK(ui.device.baseline());
    ui.status();j=status_record();
    CHECK(j["toggles_sent"]==1 && j["taps_ignored_pending"]==1 && j["displayed"]==2);
    CHECK(j["taps_physical"]==2 && j["taps_injected"]==0);
    CHECK(j["internal_heap_free"]==123456 && j["internal_heap_minimum"]==98765);
    CHECK(j["errors"]["malformed"]==3 && j["errors"]["render"].get<unsigned>()>=2);
    // Animator reads the same timestamped speaker and microphone power bridge.
    ui.tick(280);ui.tick(300);auto before=batches.size();
    dg_audio::character_signals.speaker(400,9000000);ui.tick(400);
    CHECK(batches.size()>before && batches.back().operations[0].kind==dg_display::Operation::Show);
    CHECK(!std::strcmp(batches.back().operations[0].hash,angel.mouths[angel.animator.mouth[3]].hash));
    heap_free=heap_minimum=UINT32_MAX;
    for(auto& count:ui.device.counts.errors)count=UINT32_MAX;
    ui.device.counts.toggles=ui.device.counts.retransmitted=ui.device.counts.resyncs=UINT32_MAX;
    ui.device.counts.answered=ui.device.counts.displayed=ui.device.counts.taps_ignored=UINT32_MAX;
    ui.device.counts.taps_injected=ui.device.counts.taps_physical=UINT32_MAX;
    ui.status();j=status_record();
    CHECK(j["errors"]["cancelled"]==UINT32_MAX && j["toggles_sent"]==UINT32_MAX);
    // Console and physical double-taps traverse the same pending/retry/resync path.
    struct Trace {std::vector<ClientMessage> sent;Counts counts;};
    auto run=[](bool injected){
        accept=true;rendered=dg_display::DuoCommit::Pending;
        dg::DuoUi current;current.lifecycle(true,7,0);Trace trace;ClientMessage m;
        CHECK(current.outbound.take(m));trace.sent.push_back(m);
        auto baseline=response(current);CHECK(current.receive(baseline.c_str(),false,7,0));current.tick(0);
        auto deliver=[&](int64_t now){current.tap(injected,now);current.tick(now);};
        current.touch_enabled(false);deliver(20);
        CHECK(!current.outbound.take(m) && !current.device.counts.toggles && !current.device.counts.taps_injected && !current.device.counts.taps_physical);
        current.touch_enabled(true);
        deliver(40);CHECK(current.outbound.take(m));trace.sent.push_back(m);const auto seq=m.seq;
        deliver(60);CHECK(current.device.counts.taps_ignored==1 && !current.outbound.take(m));
        for(int64_t now=2040;now<=8040;now+=2000){current.tick(now);CHECK(current.outbound.take(m));trace.sent.push_back(m);}
        CHECK(m.op==Op::Hello && m.seq>seq && current.device.pending());
        const auto old=response(current,2,"devil",seq);current.receive(old.c_str(),false,7,8050);CHECK(current.device.pending());
        const auto resync=response(current,2,"devil");current.receive(resync.c_str(),false,7,8060);CHECK(!current.device.pending());
        CHECK(current.device.character()==Persona::Devil && current.device.revision()==2 && current.device.generation()==7);
        const auto sources=current.device.counts;
        current.lifecycle(false,8,8100);current.tap(injected,8101);
        CHECK(current.device.counts.taps_injected==sources.taps_injected && current.device.counts.taps_physical==sources.taps_physical);
        current.lifecycle(true,9,8200);CHECK(current.outbound.take(m));trace.sent.push_back(m);
        current.receive(old.c_str(),false,7,8210);CHECK(!current.device.baseline() && current.device.pending());
        trace.counts=current.device.counts;current.status();
        const auto status=status_record();
        CHECK(status["taps_injected"]==(injected?2:0) && status["taps_physical"]==(injected?0:2));
        return trace;
    };
    const auto physical=run(false),injected=run(true);CHECK(physical.sent.size()==injected.sent.size());
    for(size_t i=0;i<physical.sent.size();++i){const auto& a=physical.sent[i];const auto& b=injected.sent[i];
        CHECK(a.seq==b.seq && a.generation==b.generation && a.op==b.op && a.have==b.have && !std::strcmp(a.session,b.session));}
    CHECK(physical.counts.toggles==1 && physical.counts.retransmitted==3 && physical.counts.resyncs==1);
    CHECK(injected.counts.toggles==physical.counts.toggles && injected.counts.retransmitted==physical.counts.retransmitted &&
          injected.counts.resyncs==physical.counts.resyncs && injected.counts.answered==physical.counts.answered && injected.counts.taps_ignored==physical.counts.taps_ignored);
    // Idle blink/look contention is counted separately and retries the exact
    // frame, including after the original blink would have ended.
    accept=true;rendered=dg_display::DuoCommit::Committed;
    dg::DuoUi idle;idle.lifecycle(true,11,10000);CHECK(idle.outbound.take(sent));
    CHECK(idle.receive(response(idle).c_str(),false,11,10000));idle.tick(10000);idle.tick(10020);
    const auto faults=idle.device.counts.errors[unsigned(Fault::Render)];
    busy=true;const auto before_attempts=attempts;int64_t at=10040;
    for(;at<18000 && attempts==before_attempts;at+=20)idle.tick(at);
    CHECK(attempts>before_attempts && idle.device.counts.render_busy==1);
    const auto unshown=attempted;auto diagnostic=latest("DUO_RENDER");
    CHECK(diagnostic["phase"]=="animation" && diagnostic["reason"]=="lock-busy");
    busy_error=dg_display::Error::QueueFull;idle.tick(at+400);CHECK(idle.device.counts.render_busy==2);
    CHECK(latest("DUO_RENDER")["reason"]=="queue-full");
    busy=false;busy_error=dg_display::Error::None;const auto shown=batches.size();idle.tick(at+420);
    CHECK(batches.size()==shown+1 && batches.back().count==unshown.count);
    for(unsigned i=0;i<unshown.count;++i)CHECK(batches.back().operations[i].kind==unshown.operations[i].kind &&
        batches.back().operations[i].layer==unshown.operations[i].layer &&
        !std::strcmp(batches.back().operations[i].hash,unshown.operations[i].hash));
    CHECK(idle.device.counts.errors[unsigned(Fault::Render)]==faults);
    idle.status();j=status_record();CHECK(j["render_busy"]==2 && j["errors"]["render"]==faults);
    // A genuine missing-asset rejection still increments the strict fault.
    accept=false;const auto attempt_mark=attempts;
    for(int64_t now=at+440;now<at+10000 && attempts==attempt_mark;now+=20)idle.tick(now);
    CHECK(attempts>attempt_mark && idle.device.counts.errors[unsigned(Fault::Render)]==faults+1);
    CHECK(latest("DUO_RENDER")["reason"]=="rejected");
    // Both full swaps and incremental frames escalate one continuous busy
    // period only once, and only a real admission re-arms the timeout.
    for(bool swap:{true,false}){
        auto prepare=[&](dg::DuoUi& current){
            busy=false;accept=true;rendered=dg_display::DuoCommit::Committed;
            current.lifecycle(true,12,0);CHECK(current.outbound.take(sent));
            CHECK(current.receive(response(current).c_str(),false,12,0));
            if(swap){busy=true;current.tick(0);return int64_t(0);}
            current.tick(0);current.tick(20);busy=true;
            const auto mark=attempts;
            for(int64_t now=40;now<8000;now+=20){current.tick(now);if(attempts>mark)return now;}
            CHECK(false);return int64_t(0);
        };
        dg::DuoUi transient;const auto start=prepare(transient);
        transient.tick(start+479);busy=false;transient.tick(start+499);
        CHECK(transient.device.counts.errors[unsigned(Fault::Render)]==0);
        dg::DuoUi wedged;const auto since=prepare(wedged);
        wedged.tick(since+500);CHECK(wedged.device.counts.errors[unsigned(Fault::Render)]==0);
        wedged.tick(since+600);CHECK(wedged.device.counts.errors[unsigned(Fault::Render)]==1);
        CHECK(latest("DUO_RENDER")["reason"]=="busy-timeout" &&
              latest("DUO_RENDER")["phase"]==(swap?"swap":"animation"));
        const auto retained=attempted;wedged.tick(since+1200);
        CHECK(wedged.device.counts.errors[unsigned(Fault::Render)]==1);
        busy=false;wedged.tick(since+1220);
        CHECK(batches.back().count==retained.count);
        for(unsigned i=0;i<retained.count;++i)CHECK(!std::strcmp(batches.back().operations[i].hash,retained.operations[i].hash));
        // Start another pending swap/frame after successful admission.
        busy=true;int64_t again=since+1240;
        if(swap){CHECK(wedged.receive(response(wedged,2,"devil").c_str(),false,12,again));wedged.tick(again);}
        else{
            const auto mark=attempts;
            for(;again<since+10000;again+=20){wedged.tick(again);if(attempts>mark)break;}
            CHECK(attempts>mark);
        }
        wedged.tick(again+600);CHECK(wedged.device.counts.errors[unsigned(Fault::Render)]==2);
        CHECK(latest("DUO_RENDER")["reason"]=="busy-timeout");
        wedged.tick(again+1800);CHECK(wedged.device.counts.errors[unsigned(Fault::Render)]==2);
    }
    busy=false;
    // Production formatter at all wire-field maxima, including 128-byte conn,
    // 16-byte generated boot session, u32 counters/seq and u64 revisions.
    accept=true;rendered=dg_display::DuoCommit::Committed;scene_revision=UINT64_MAX;
    dg::DuoUi worst;worst.lifecycle(true,UINT32_MAX,0);CHECK(worst.outbound.take(sent));
    auto maximum=nlohmann::json::parse(response(worst));maximum["conn"]=std::string(128,'x');
    CHECK(worst.receive(maximum.dump().c_str(),false,UINT32_MAX,0));
    maximum["seq"]=UINT32_MAX;maximum["rev"]=UINT64_MAX;
    CHECK(worst.receive(maximum.dump().c_str(),false,UINT32_MAX,0));
    const auto start=lines.size();worst.tick(0);worst.tick(20);
    auto& c=worst.device.counts;c.toggles=c.retransmitted=c.resyncs=c.answered=c.displayed=c.taps_ignored=c.taps_injected=c.taps_physical=c.render_busy=UINT32_MAX;
    for(auto& value:c.errors)value=UINT32_MAX;
    worst.status();j=proof_record();CHECK(j["seq"]==UINT32_MAX && j["rev"]==UINT64_MAX && j["scene_revision"]==UINT64_MAX);
    CHECK(j["conn"].get<std::string>().size()==128 && j["session"].get<std::string>().size()==16);
    unsigned headers=0,layers=0,statuses=0,errors=0;size_t longest=0;
    for(size_t i=start;i<lines.size();++i){
        CHECK(lines[i].size()<=dg::DuoRecordLimit && lines[i].size()*100<=496*85);
        longest=std::max(longest,lines[i].size());
        headers+=lines[i].find("DUO_PROOF {")!=std::string::npos;
        layers+=lines[i].find("DUO_PROOF_LAYER ")!=std::string::npos;
        statuses+=lines[i].find("DUO_STATUS ")!=std::string::npos;
        errors+=lines[i].find("DUO_ERRORS ")!=std::string::npos;
        CHECK(nlohmann::json::parse(lines[i].substr(lines[i].find('{'))).is_object());
    }
    CHECK(headers==1 && layers==3 && statuses==2 && errors==4);
    for(const auto& line:lines)CHECK(line.size()<=dg::DuoRecordLimit);
    std::printf("duo UI parser, touch, split proof/status passed; worst record %zu bytes (budget %zu)\n",longest,dg::DuoRecordLimit);
}
