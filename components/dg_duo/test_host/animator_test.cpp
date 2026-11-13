#include "dg_duo/animator.h"
#include "dg_audio/character_signals.h"
#include "check.h"
#include <cstring>
#include <vector>
using namespace dg_duo;
namespace {
struct Sink {
    std::vector<dg_display::Request> batches;bool accept=true;
    static bool send(void* context,const dg_display::Request& q){auto& self=*static_cast<Sink*>(context);
        CHECK(q.kind==dg_display::RequestKind::Scene && q.count>0 && q.count<=8);
        unsigned mask=0;for(unsigned i=0;i<q.count;++i){CHECK(q.operations[i].layer<8);CHECK(!(mask&(1u<<q.operations[i].layer)));mask|=1u<<q.operations[i].layer;}
        if(!self.accept)return false;
        self.batches.push_back(q);return true;
    }
};
void show(const dg_display::SceneOperation& op,unsigned layer,const Frame& frame){
    CHECK(op.kind==dg_display::Operation::Show && op.layer==layer);
    CHECK(!std::strcmp(op.hash,frame.hash) && op.x==frame.x && op.y==frame.y);
}
}
int main(){
    for(auto character:{Character::Angel,Character::Devil}){
        const auto& p=character==Character::Angel?angel:devil;
        Sink sink;DuoAnimator a(Sink::send,&sink);CHECK(!a.tick(0,0,false,0));
        CHECK(a.set_active(character,1000));CHECK(sink.batches.size()==1);
        const auto& batch=sink.batches.back();CHECK(batch.count==8);
        show(batch.operations[0],0,p.faceplate);show(batch.operations[1],1,p.eyes[p.animator.open]);
        show(batch.operations[2],2,p.mouths[p.animator.mouth[0]]);
        for(unsigned i=3;i<8;++i)CHECK(batch.operations[i].kind==dg_display::Operation::Hide && batch.operations[i].layer==i);
        CHECK(a.next_blink()>=4000 && a.next_blink()<=7000);
        // Silence stays at rest; RMS ranges settle to the five ordered levels.
        for(unsigned level=0;level<5;++level){
            constexpr uint32_t powers[]={0,300*300,1000*1000,1800*1800,3000*3000};
            CHECK(a.set_active(character,0));
            for(int64_t now=0;now<=2000;now+=20)CHECK(a.tick(now,powers[level],false,0));
            CHECK(a.mouth_level()==level);
            if(level)show(sink.batches.back().operations[0],2,p.mouths[p.animator.mouth[level]]);
        }
        // A mic-only sample never moves the mouth.
        CHECK(a.set_active(character,0));dg_audio::CharacterSignals signals;signals.capture(20,1000000,true);
        auto sample=signals.read(20);CHECK(a.tick(20,sample.speaker_power,sample.mic_open,sample.mic_power));CHECK(!a.mouth_level());
        // Exact open -> half -> closed -> half -> open timings.
        CHECK(a.set_active(character,0));const auto due=a.next_blink();
        CHECK(a.tick(due-1,0,false,0));CHECK(a.tick(due,0,false,0));CHECK(a.eye_frame()==p.animator.half);
        CHECK(a.tick(due+59,0,false,0));CHECK(a.eye_frame()==p.animator.half);
        CHECK(a.tick(due+60,0,false,0));CHECK(a.eye_frame()==p.animator.blink);
        CHECK(a.tick(due+120,0,false,0));CHECK(a.eye_frame()==p.animator.half);
        CHECK(a.tick(due+180,0,false,0));CHECK(a.eye_frame()==p.animator.open);
        // A scene swap discards both the in-flight blink and held speaking mouth.
        CHECK(a.tick(due+280,100000000,false,0));CHECK(a.mouth_level());
        const auto before=sink.batches.size();CHECK(a.set_active(character,due+290));
        CHECK(sink.batches.size()==before+1 && sink.batches.back().count==8);
        CHECK(a.mouth_level()==0 && a.eye_frame()==p.animator.open);
        // A look eventually returns to open. Speaking interrupts it immediately.
        CHECK(a.set_active(character,0));int64_t look=0;
        for(;look<10000;look+=20){CHECK(a.tick(look,0,false,0));
            if(a.eye_frame()==p.animator.look_left || a.eye_frame()==p.animator.look_right)break;}
        CHECK(look<10000);
        // Let the look expire without speaker input, before another look starts.
        bool returned=false;
        for(int64_t now=look+20;now<=look+600;now+=20){
            CHECK(a.tick(now,0,false,0));
            if(a.eye_frame()==p.animator.open){returned=true;break;}
        }
        CHECK(returned && !a.mouth_level());
        // Enter a fresh look and interrupt it with speaking.
        CHECK(a.set_active(character,0));look=0;
        for(;look<10000;look+=20){CHECK(a.tick(look,0,false,0));
            if(a.eye_frame()==p.animator.look_left || a.eye_frame()==p.animator.look_right)break;}
        CHECK(look<10000);
        CHECK(a.tick(look+20,1000000,false,0));CHECK(a.eye_frame()==p.animator.open);
        // Mouth updates hold for >=100ms; silence closes after the audible hold.
        CHECK(a.set_active(character,0));CHECK(a.tick(0,1000000,false,0));auto mouth=a.mouth_level();
        CHECK(a.tick(99,100000000,false,0));CHECK(a.mouth_level()==mouth);
        CHECK(a.tick(100,100000000,false,0));CHECK(a.mouth_level()>mouth);
        CHECK(a.tick(200,0,false,0));CHECK(a.mouth_level()==0);
    }
    Sink sink;DuoAnimator a(Sink::send,&sink);CHECK(a.set_active(Character::Angel));
    sink.accept=false;CHECK(!a.set_active(Character::Devil));CHECK(a.active()==Character::Angel);
    sink.accept=true;CHECK(a.set_active(Character::Devil));show(sink.batches.back().operations[0],0,devil.faceplate);
    // A failed incremental feature admission retries the unshown frame.
    CHECK(a.set_active(Character::Angel,0));const auto accepted=sink.batches.size();
    sink.accept=false;CHECK(!a.tick(100,9000000,false,0));CHECK(sink.batches.size()==accepted);
    sink.accept=true;CHECK(a.tick(120,9000000,false,0));CHECK(sink.batches.size()==accepted+1);
    show(sink.batches.back().operations[0],2,angel.mouths[angel.animator.mouth[3]]);
    // Long mixed signal workload exercises every emitted batch's bounds.
    for(int64_t now=0;now<600000;now+=20){if(now%10000==0)CHECK(a.set_active(now%20000?Character::Devil:Character::Angel,now));
        CHECK(a.tick(now,now%5000<2500?9000000:0,true,100000));}
}
