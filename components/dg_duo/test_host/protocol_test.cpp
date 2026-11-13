#include "dg_duo/protocol.h"
#include "check.h"
#include <cstring>
#include <cstdio>
#include <vector>
using namespace dg_duo;
struct Harness {
    std::vector<ClientMessage> sent;bool admit=true;
    Device d{0x1234,Persona::Angel,[](void* p,const ClientMessage& m){auto& h=*static_cast<Harness*>(p);if(!h.admit)return false;h.sent.push_back(m);return true;},this};
    Outcome outcome(uint64_t rev,Persona c=Persona::Angel,const char* conn="epoch",bool state=true){
        Outcome o;std::strcpy(o.session,d.session());std::strcpy(o.conn,conn);o.seq=d.request().seq;o.rev=rev;o.character=c;o.state=state;return o;
    }
    void ready(){d.connect(1,0);CHECK(d.receive(outcome(1),1));}
};
// Step 3 traces: assert every counter, including the absence of unrelated faults.
static void auto_check(const char* trace,const Device& d,Persona character,uint64_t rev,
                       bool pending,unsigned answered,unsigned toggles,
                       unsigned stale=0,unsigned failed=0,unsigned session=0,
                       unsigned connection=0,unsigned baseline=0){
    CHECK(d.character()==character && d.revision()==rev && d.pending()==pending);
    CHECK(d.counts.answered==answered && d.counts.toggles==toggles);
    CHECK(d.counts.retransmitted==0 && d.counts.resyncs==0 && d.counts.displayed==0);
    CHECK(d.counts.taps_ignored==0 && d.counts.taps_injected==0 && d.counts.taps_physical==0 && d.counts.render_busy==0);
    for(unsigned i=0;i<unsigned(Fault::Count);++i){
        unsigned expected=i==unsigned(Fault::Stale)?stale:i==unsigned(Fault::SwitchFailed)?failed:
            i==unsigned(Fault::Session)?session:i==unsigned(Fault::Connection)?connection:
            i==unsigned(Fault::Baseline)?baseline:0;
        CHECK(d.counts.errors[i]==expected);
    }
    std::printf("auto trace %s: character=%s rev=%llu pending=%u answered=%u toggles=%u stale=%u switch-failed=%u session=%u connection=%u baseline=%u\n",
        trace,name(character),(unsigned long long)rev,unsigned(pending),answered,toggles,stale,failed,session,connection,baseline);
}
static Outcome addressed(Outcome o){std::strcpy(o.reason,"addressed");return o;}
static void auto_traces(){
    // 1. Answered toggle, then unsolicited state at the high-water sequence.
    {
        Harness h;h.ready();h.d.tap(1);CHECK(h.d.receive(h.outcome(2,Persona::Devil),1));
        CHECK(h.d.receive(addressed(h.outcome(3)),1));
        auto_check("1",h.d,Persona::Angel,3,false,2,1);
    }
    // 2. Lost toggle answer: auto settles it; later cached state is stale.
    {
        Harness h;h.ready();h.d.tap(1);auto cached=h.outcome(2,Persona::Devil);
        CHECK(h.d.receive(addressed(h.outcome(3)),1));
        auto_check("2-auto",h.d,Persona::Angel,3,false,2,1);
        CHECK(!h.d.receive(cached,1));
        auto_check("2-replay",h.d,Persona::Angel,3,false,2,1,1);
    }
    // 3. Lost error answer: auto settles the pending toggle without a fault.
    {
        Harness h;h.ready();h.d.tap(1);
        auto lost=h.outcome(1,Persona::Angel,"epoch",false);std::strcpy(lost.reason,"switch-failed");
        CHECK(lost.seq==h.d.request().seq && !lost.state);
        CHECK(h.d.receive(addressed(h.outcome(2,Persona::Devil)),1));
        auto_check("3",h.d,Persona::Devil,2,false,2,1);
    }
    // 4. Auto for hw cannot complete a newer pending toggle (hw+1).
    {
        Harness h;h.ready();h.d.tap(1);CHECK(h.d.receive(h.outcome(2,Persona::Devil),1));
        auto next=addressed(h.outcome(3));h.d.tap(2);
        CHECK(next.seq+1==h.d.request().seq && h.d.receive(next,1));
        auto_check("4-pending",h.d,Persona::Angel,3,true,2,2);
        CHECK(h.d.receive(h.outcome(4,Persona::Devil),1));
        auto_check("4-done",h.d,Persona::Devil,4,false,3,2);
    }
    // 5. A rolled session and an old connection both reject the auto state.
    {
        Harness h;h.ready();auto old=addressed(h.outcome(2,Persona::Devil));
        Device rolled(0x1235,Persona::Angel,[](void*,const ClientMessage&){return true;},nullptr);
        rolled.connect(1,0);CHECK(!rolled.receive(old,1));
        auto current=old;std::strcpy(current.session,rolled.session());std::strcpy(current.conn,"new");
        current.rev=1;current.character=Persona::Angel;CHECK(rolled.receive(current,1));
        std::strcpy(old.session,rolled.session());CHECK(!rolled.receive(old,1));
        auto_check("5",rolled,Persona::Angel,1,false,1,0,0,0,1,1);
    }
    // 6. The establishing exception accepts revision-zero adoption failure.
    {
        Harness h;h.d.connect(1,0);auto first=h.outcome(0);std::strcpy(first.reason,"adoption-failed");
        CHECK(h.d.receive(first,1) && h.d.baseline());
        CHECK(h.d.receive(addressed(h.outcome(1,Persona::Devil)),1));
        auto_check("6",h.d,Persona::Devil,1,false,1,0);
    }
    // 7. Lost hello: same-seq auto establishes; cached hello is then stale.
    {
        Harness h;h.d.connect(1,0);auto hello=h.outcome(1);
        CHECK(h.d.receive(addressed(h.outcome(2,Persona::Devil)),1));
        CHECK(h.d.baseline() && !std::strcmp(h.d.connection(),"epoch"));
        auto_check("7-auto",h.d,Persona::Devil,2,false,1,0);
        CHECK(!h.d.receive(hello,1));
        auto_check("7-replay",h.d,Persona::Devil,2,false,1,0,1);
    }
    {
        Harness h;h.d.connect(4,0);auto o=addressed(h.outcome(2,Persona::Devil));++o.seq;
        CHECK(!h.d.receive(o,4));--o.seq;
        std::strcpy(o.session,"other");CHECK(!h.d.receive(o,4));std::strcpy(o.session,h.d.session());
        CHECK(!h.d.receive(o,3));CHECK(!h.d.baseline());
        auto_check("7-negative",h.d,Persona::Angel,0,true,0,0,0,0,2,0,1);
        CHECK(h.d.receive(o,4));
        auto_check("7-recovered",h.d,Persona::Devil,2,false,1,0,0,0,2,0,1);
    }
    // 8. Cached hello and toggle replays after auto cannot change the face.
    for(bool toggle:{false,true}){
        Harness h;h.d.connect(1,0);auto cached=h.outcome(1);CHECK(h.d.receive(cached,1));
        if(toggle){h.d.tap(1);cached=h.outcome(2,Persona::Devil);CHECK(h.d.receive(cached,1));}
        auto next=addressed(h.outcome(toggle?3:2,toggle?Persona::Angel:Persona::Devil));
        CHECK(h.d.receive(next,1));CHECK(!h.d.receive(cached,1));
        auto_check(toggle?"8-toggle":"8-hello",h.d,toggle?Persona::Angel:Persona::Devil,
                   toggle?3:2,false,toggle?2:1,toggle?1:0,1);
    }
    // 9. A delayed error is still counted after auto completed its request.
    {
        Harness h;h.ready();h.d.tap(1);auto cached=h.outcome(1,Persona::Angel,"epoch",false);
        std::strcpy(cached.reason,"switch-failed");
        CHECK(h.d.receive(addressed(h.outcome(2,Persona::Devil)),1));
        CHECK(!h.d.receive(cached,1));
        auto_check("9",h.d,Persona::Devil,2,false,2,1,0,1);
    }
    // 10. Equal and older revs complete matching seqs without being applied.
    {
        Harness h;h.ready();CHECK(h.d.receive(addressed(h.outcome(3,Persona::Devil)),1));h.d.tap(1);
        CHECK(!h.d.receive(h.outcome(3),1));
        auto_check("10-equal",h.d,Persona::Devil,3,false,2,1);
        h.d.tap(2);CHECK(!h.d.receive(h.outcome(2),1));
        auto_check("10-older",h.d,Persona::Devil,3,false,3,2,1);
    }
}
int main(){
    // Mirror DeviceView's lost acknowledgements table (1..4 sends dropped).
    for(unsigned lost=1;lost<=4;++lost){
        Harness h;h.ready();h.d.tap(1);auto request=h.d.request();auto original=h.outcome(2,Persona::Devil);
        for(unsigned attempt=0;attempt<4;++attempt){
            if(attempt>=lost){CHECK(h.d.receive(original,1));break;}
            h.d.tick(2001+attempt*2000);
        }
        if(h.d.pending()){
            CHECK(h.d.request().op==Op::Hello && h.d.request().seq>request.seq);
            CHECK(h.d.request().have==Persona::Angel);
            CHECK(h.d.receive(original,1));CHECK(h.d.pending());
            CHECK(!h.d.receive(h.outcome(2,Persona::Devil),1));
        }
        CHECK(!h.d.pending() && h.d.character()==Persona::Devil && h.d.revision()==2);
        for(const auto& s:h.sent)if(s.seq==request.seq){CHECK(s.op==request.op && s.have==request.have);}
    }
    // Reconnect, pending toggle, lower baseline, old generation/epoch, revert.
    {
        Harness h;h.ready();h.d.tap(10);auto late=h.outcome(6,Persona::Devil);h.d.receive(late,1);
        auto revert=h.outcome(7);CHECK(h.d.receive(revert,1));h.d.tap(11);
        h.d.connect(2,12);CHECK(h.d.request().have==Persona::Angel);
        auto first=h.outcome(1,Persona::Angel,"new");CHECK(h.d.receive(first,2));
        CHECK(!h.d.receive(late,1));CHECK(!h.d.receive(late,2));
        h.d.tap(13);CHECK(h.d.receive(h.outcome(2,Persona::Devil,"new"),2));
    }
    // Equal revision settles matching seq; old seq may update but never settle.
    {
        Harness h;h.ready();h.d.tap(5);CHECK(!h.d.receive(h.outcome(1),1));CHECK(!h.d.pending());
        h.d.tap(6);auto other=h.outcome(2,Persona::Devil);--other.seq;
        CHECK(h.d.receive(other,1) && h.d.pending());CHECK(!h.d.receive(h.outcome(2,Persona::Devil),1));
        CHECK(!h.d.pending());h.d.tap(7);auto stale=h.outcome(1);CHECK(!h.d.receive(stale,1));CHECK(!h.d.pending());
    }
    // Lost errors remain errors; pending taps counted, no extra sequence sent.
    {
        Harness h;h.ready();h.d.tap(10);auto seq=h.d.request().seq;
        h.d.tap(11);h.d.tap(12);CHECK(h.d.counts.taps_ignored==2 && h.d.request().seq==seq);
        auto error=h.outcome(1,Persona::Angel,"epoch",false);std::strcpy(error.reason,"switch-failed");
        CHECK(!h.d.receive(error,1));CHECK(!h.d.pending());CHECK(h.d.character()==Persona::Angel);
        CHECK(h.d.counts.errors[unsigned(Fault::SwitchFailed)]==1);
    }
    // No baseline until the first matching hello state; revision zero is legal.
    {
        Harness h;h.d.connect(4,0);auto o=h.outcome(0);++o.seq;
        CHECK(!h.d.receive(o,4) && !h.d.baseline());--o.seq;
        std::strcpy(o.session,"another");CHECK(!h.d.receive(o,4));std::strcpy(o.session,h.d.session());
        CHECK(h.d.receive(o,4) && h.d.baseline());
        h.d.tap(1);CHECK(!h.d.receive(h.outcome(0),4));
    }
    // Exactly three retries 2s apart, then one resync with the same retry bound.
    {
        Harness h;h.ready();h.d.tap(1);auto seq=h.d.request().seq;
        for(unsigned t=1;t<=3;++t){h.d.tick(t*2000);CHECK(h.sent.size()==t+1);h.d.tick(t*2000+1);CHECK(h.sent.size()==t+2);}
        h.d.tick(8001);CHECK(h.d.request().seq==seq+1 && h.d.request().op==Op::Hello);
        for(unsigned t=1;t<=4;++t)h.d.tick(8001+t*2000);
        CHECK(!h.d.pending() && h.d.counts.resyncs==1 && h.d.counts.retransmitted==6);
        auto count=h.sent.size();h.d.tap(17000);h.d.tick(99999);CHECK(h.sent.size()==count);
        h.d.connect(5,100000);CHECK(h.d.pending() && h.sent.size()==count+1);
    }
    // Defensive hello errors use new sequences and bounded 1/2/4-second backoff.
    {
        Harness h;h.ready();h.d.tap(1);
        for(unsigned t=1;t<=4;++t)h.d.tick(1+t*2000);
        auto error=h.outcome(1,Persona::Angel,"epoch",false);std::strcpy(error.reason,"deadline");
        CHECK(!h.d.receive(error,1,8001) && h.d.pending());
        auto n=h.sent.size();h.d.tap(8500);CHECK(h.sent.size()==n && h.d.counts.taps_ignored==1);
        int64_t now=8001;uint32_t seq=h.d.request().seq;
        for(unsigned retry=0;retry<3;++retry){
            const auto backoff=1000ll<<retry;
            h.d.tick(now+backoff-1);CHECK(h.sent.size()==n);
            now+=backoff;h.d.tick(now);CHECK(h.sent.size()==++n && h.d.request().seq==++seq);
            CHECK(h.d.request().op==Op::Hello);
            error=h.outcome(1,Persona::Angel,"epoch",false);std::strcpy(error.reason,"deadline");
            CHECK(!h.d.receive(error,1,now));
        }
        CHECK(!h.d.pending());h.d.tick(99999);h.d.tap(100000);CHECK(h.sent.size()==n);
        CHECK(h.d.counts.errors[unsigned(Fault::Deadline)]==4);
        h.d.connect(2,100001);CHECK(h.d.pending() && h.sent.size()==n+1);
    }
    // Adoption failure is a normal revision-zero baseline, not an error wait.
    {
        Harness h;h.d.connect(1,0);auto state=h.outcome(0,Persona::Devil);
        std::strcpy(state.reason,"adoption-failed");CHECK(h.d.receive(state,1,0));
        CHECK(h.d.baseline() && !h.d.pending() && h.d.character()==Persona::Devil);
        h.d.tap(1);CHECK(h.d.request().op==Op::Toggle);
    }
    // First-hello errors recover without a connection change; old seq cannot settle.
    {
        Harness h;h.d.connect(3,0);auto old=h.outcome(0,Persona::Angel,"epoch",false);
        std::strcpy(old.reason,"voice");CHECK(!h.d.receive(old,3,10) && h.d.pending());
        h.d.tick(1009);CHECK(h.sent.size()==1);h.d.tick(1010);CHECK(h.sent.size()==2);
        CHECK(h.d.request().seq==2 && !h.d.baseline());
        auto stale=h.outcome(1,Persona::Devil);stale.seq=1;CHECK(!h.d.receive(stale,3,1011) && h.d.pending());
        auto state=h.outcome(1,Persona::Devil);CHECK(h.d.receive(state,3,1012));CHECK(!h.d.pending());
        h.d.tap(1013);CHECK(h.d.request().op==Op::Toggle && h.d.generation()==3);
        // A new generation cancels even a scheduled hello retry.
        h.d.connect(4,2000);old=h.outcome(1,Persona::Devil,"epoch",false);h.d.receive(old,4,2001);
        const auto count=h.sent.size();h.d.disconnect(5);h.d.tick(100000);CHECK(h.sent.size()==count && !h.d.pending());
    }
    // A saved character is the RAM character advertised by the first hello.
    {
        ClientMessage first;
        Device d(1,Persona::Devil,[](void* p,const ClientMessage& m){*static_cast<ClientMessage*>(p)=m;return true;},&first);
        d.connect(5,0);CHECK(first.op==Op::Hello && first.have==Persona::Devil && first.seq==1);
    }
    // Mailbox overload, admission retry, stale generation and Ready fence.
    {
        Mailbox m;ClientMessage a,b;a.generation=8;a.seq=42;
        CHECK(m.post(a));CHECK(!m.post(a));CHECK(m.take(b));CHECK(!m.take(b));CHECK(b.seq==42);
        CHECK(Mailbox::current(b,8,8,true));CHECK(!Mailbox::current(b,7,8,true));
        CHECK(!Mailbox::current(b,8,9,true));CHECK(!Mailbox::current(b,8,8,false));
        Harness h;h.admit=false;h.d.connect(1,0);CHECK(h.d.pending() && h.sent.empty());
        h.admit=true;h.d.tick(20);CHECK(h.sent.size()==1 && h.d.counts.retransmitted==0);
    }
    // Deferred acknowledgements coalesce; a session never invokes the writer.
    {
        DeferredCharacter p;unsigned writes=0;Persona stored=Persona::Angel;
        auto store=[&](Persona c){++writes;stored=c;return true;};
        p.acknowledge(Persona::Devil);for(unsigned n=0;n<100;++n)CHECK(!p.flush(true,store));CHECK(!writes);
        CHECK(!p.flush(false,[](Persona){return false;}) && p.pending());
        p.acknowledge(Persona::Angel);CHECK(p.flush(false,store) && writes==1 && stored==Persona::Angel);
        CHECK(!p.flush(false,store));
        p.acknowledge(Persona::Devil);
        CHECK(p.flush(false,[&](Persona c){p.acknowledge(Persona::Angel);return store(c);}));
        CHECK(p.pending());CHECK(p.flush(false,store) && stored==Persona::Angel);
    }
    auto_traces();
    std::puts("duo device protocol table, mailbox and deferred persistence passed");
}
