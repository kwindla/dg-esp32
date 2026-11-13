#include "dg_display/duo_commit.h"
#include "check.h"
#include <cstdio>
#include <cstring>
using namespace dg_display;
int main(){
    Cache cache;FakeClock clock;Engine engine(cache,clock);DuoRendered rendered;
    unsigned sequence=0;
    auto post=[&]{Request q;q.meta.epoch=engine.epoch;q.meta.sequence=++sequence;
        std::snprintf(q.meta.id,sizeof(q.meta.id),"duo-scene-%u",sequence);q.count=1;
        q.operations[0].kind=Operation::Fill;q.operations[0].color=sequence;
        CHECK(engine.post(q,0,true).admission==Admission::Accepted);return q;
    };
    auto poll=[&](const Request& q){return rendered.poll(engine.result(q.meta.id),{q.meta.epoch,q.meta.sequence},engine.epoch);};
    auto q=post();CHECK(poll(q)==DuoCommit::Pending);
    engine.tick(0);CHECK(poll(q)==DuoCommit::Pending);RenderLease lease;CHECK(engine.snapshot(lease));
    // A failed render cannot manufacture a proof even if it was admitted/applied.
    auto current=engine.current(lease);engine.complete(lease,false,0);rendered.complete(lease,false,current);
    CHECK(poll(q)==DuoCommit::Pending);
    CHECK(engine.snapshot(lease));current=engine.current(lease);engine.complete(lease,true,360*360);
    rendered.complete(lease,true,current);CHECK(poll(q)==DuoCommit::Committed);
    // A later producer's successful render cannot hide our earlier committed result.
    auto later=post();engine.tick(1);CHECK(engine.snapshot(lease));
    current=engine.current(lease);engine.complete(lease,true,360*360);rendered.complete(lease,true,current);
    CHECK(rendered.revision>engine.result(q.meta.id)->revision);
    CHECK(poll(q)==DuoCommit::Committed && poll(later)==DuoCommit::Committed);
    // Two coalesced scene batches yield only the final committed revision.
    auto superseded=post();engine.tick(1);auto final=post();engine.tick(2);
    CHECK(engine.snapshot(lease));current=engine.current(lease);engine.complete(lease,true,360*360);
    rendered.complete(lease,true,current);CHECK(poll(superseded)==DuoCommit::Failed && poll(final)==DuoCommit::Committed);
    // Supersession before application, and an epoch change during rendering.
    superseded=post();final=post();CHECK(poll(superseded)==DuoCommit::Failed);
    engine.tick(3);CHECK(engine.snapshot(lease));engine.start_session();
    current=engine.current(lease);engine.complete(lease,true,360*360);rendered.complete(lease,true,current);
    CHECK(!current && poll(final)==DuoCommit::Failed);
    // Unrendered Completed results (e.g. offscreen/no dirty work) cannot prove.
    Result fake;fake.state=ResultState::Completed;fake.revision=999;
    CHECK(rendered.poll(&fake,{engine.epoch,1},engine.epoch)==DuoCommit::Failed);
    std::puts("duo proof with real scene engine: commit/failure/supersession/epoch passed");
}
