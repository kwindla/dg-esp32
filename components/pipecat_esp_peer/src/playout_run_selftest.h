#pragma once
#include "playout.h"
#include <memory>
#include <stdexcept>
namespace pipecat::esp_peer {
// Test-only storage: never put the 48KB payload store on the console stack.
// Shared by playback-selftest and native tests; no production-path allocation.
inline unsigned playout_run_selftest() {
    auto storage=std::make_unique<Playout::Storage>();
    auto require=[](bool ok) {if(!ok)throw std::runtime_error("missing-run accounting");};
    uint8_t payload[2]{0x48,42};unsigned checked=0;
    for(unsigned n=1;n<=7;++n) {
        auto p=std::make_unique<Playout>(*storage);p->authorize(7,1);
        auto put=[&](unsigned seq) {require(p->insert({7,seq*960,uint16_t(seq),false,payload,2},0));};
        auto tick=[&](unsigned i) {
            dg::RenderTick t;t.valid=true;t.index=i;t.deadline_us=int64_t(Playout::target_frames)*20000+i*20000;t.device_epoch=1;
            auto d=p->pull(t,true);p->finish(d,d.kind==Playout::Kind::Decode);return d;
        };
        put(0);require(tick(0).kind==Playout::Kind::Decode);put(n+1);
        for(unsigned i=1;i<=n;++i)tick(i);
        require(p->snapshot(0).counters.missing_run_ended==std::array<uint64_t,6>{});
        require(tick(n+1).kind==Playout::Kind::Decode);
        std::array<uint64_t,6> expected{};expected[n<6?n-1:5]=1;
        require(p->snapshot(0).counters.missing_run_ended==expected);
        tick(n+2);require(p->cancel(int64_t(Playout::target_frames)*20000+(n+3)*20000));++expected[0];
        require(p->snapshot(0).counters.missing_run_ended==expected);
        p->disconnect();require(p->snapshot(0).counters.missing_run_ended==expected);
        ++checked;
    }
    return checked;
}
}
