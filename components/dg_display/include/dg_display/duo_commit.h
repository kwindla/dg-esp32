#pragma once
#include "engine.h"
namespace dg_display {
struct DuoTicket {uint64_t epoch=0,number=0;};
enum class DuoCommit {Pending,Committed,Failed};
// Only the display owner's successful, current render path advances this record.
struct DuoRendered {
    uint64_t epoch=0,revision=0;
    void complete(const RenderLease& lease,bool success,bool current){
        if(success && current){epoch=lease.epoch;revision=lease.scene.revision;}
    }
    DuoCommit poll(const Result* result,DuoTicket ticket,uint64_t engine_epoch)const{
        if(ticket.epoch!=engine_epoch || !result)return DuoCommit::Failed;
        if(result->state==ResultState::Pending || result->state==ResultState::Applied)return DuoCommit::Pending;
        return result->state==ResultState::Completed && epoch==ticket.epoch && revision>=result->revision
            ?DuoCommit::Committed:DuoCommit::Failed;
    }
};
}
