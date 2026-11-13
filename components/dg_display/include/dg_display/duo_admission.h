#pragma once
#include "duo_commit.h"
#include <cstdio>
namespace dg_display {
enum class DuoAdmission:uint8_t {Accepted,Busy,Rejected};
struct DuoPostResult {DuoAdmission admission=DuoAdmission::Rejected;Error error=Error::Malformed;};
// Duo's zero-wait bridge, shared with native engine/lock-contention regression.
// Busy leaves the animator's unshown frame pending; only admission advances it.
template<class TryLock,class Unlock,class Clock>
DuoPostResult duo_admit_batch(const Request& batch,DuoTicket* ticket,Engine& engine,uint64_t& sequence,
                             TryLock try_lock,Unlock unlock,Clock now){
    if(batch.kind!=RequestKind::Scene || !batch.count || batch.count>8)return {DuoAdmission::Rejected,Error::Malformed};
    if(!try_lock())return {DuoAdmission::Busy,Error::None};
    struct Release {Unlock& fn;~Release(){fn();}} release{unlock};
    Request q=batch;q.meta.epoch=engine.epoch;q.meta.sequence=++sequence;
    std::snprintf(q.meta.id,sizeof(q.meta.id),"duo-scene-%llu",(unsigned long long)sequence);
    const auto result=engine.post(q,now(),true);
    if(result.admission==Admission::Accepted){
        if(ticket)*ticket={q.meta.epoch,sequence};
        return {DuoAdmission::Accepted,Error::None};
    }
    return {result.admission==Admission::Full && result.error==Error::QueueFull?DuoAdmission::Busy:DuoAdmission::Rejected,result.error};
}
}
