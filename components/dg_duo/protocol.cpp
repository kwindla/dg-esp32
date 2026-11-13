#include "dg_duo/protocol.h"
#include <cstdio>
#include <cstring>
#include <limits>
namespace dg_duo {
const char* fault_name(Fault f){
    constexpr const char* names[]={"malformed","session","connection","baseline","stale-revision","mailbox","send","timeout","render","switch-failed","mismatch","stale","voice","deadline","cancelled","other"};
    return names[unsigned(f)];
}
bool Mailbox::post(const ClientMessage& m)noexcept{
    unsigned empty=0;if(!state_.compare_exchange_strong(empty,1,std::memory_order_acquire))return false;
    item_=m;state_.store(2,std::memory_order_release);return true;
}
bool Mailbox::take(ClientMessage& m)noexcept{
    unsigned full=2;if(!state_.compare_exchange_strong(full,3,std::memory_order_acquire))return false;
    m=item_;state_.store(0,std::memory_order_release);return true;
}
Device::Device(uint64_t boot,Persona initial,Send send,void* context):send_(send),context_(context),boot_(boot),character_(initial){
    std::snprintf(pending_.session,sizeof(pending_.session),"%016llx",(unsigned long long)boot_);
}
void Device::begin(Op op,int64_t now,bool resync){
    if(seq_==std::numeric_limits<uint32_t>::max()){
        // One boot nonce plus a wrap epoch; preserve uniqueness across all ops.
        ++boot_;seq_=0;conn_[0]=0;rev_=0;op=Op::Hello;
        std::snprintf(pending_.session,sizeof(pending_.session),"%016llx",(unsigned long long)boot_);
    }
    pending_.generation=generation_;pending_.seq=++seq_;pending_.op=op;pending_.have=character_;
    awaiting_=true;resync_=resync;attempts_=0;due_=now;tick(now);
}
void Device::connect(uint32_t generation,int64_t now){
    generation_=generation;conn_[0]=0;rev_=0;awaiting_=false;live_=true;waiting_connect_=false;hello_retry_=false;hello_errors_=0;
    begin(Op::Hello,now);
}
void Device::disconnect(uint32_t generation){generation_=generation;conn_[0]=0;rev_=0;awaiting_=false;hello_retry_=false;live_=false;}
void Device::tap(int64_t now){
    if(pending()){++counts.taps_ignored;return;}
    if(live_ && baseline() && !waiting_connect_)begin(Op::Toggle,now);
}
void Device::tick(int64_t now){
    if(live_ && hello_retry_){
        if(now<due_)return;
        hello_retry_=false;begin(Op::Hello,now,resync_);return;
    }
    if(!live_ || !awaiting_ || now<due_)return;
    if(attempts_==4){
        error(Fault::Timeout);
        if(resync_){awaiting_=false;waiting_connect_=true;return;}
        ++counts.resyncs;begin(Op::Hello,now,true);return;
    }
    if(!send_(context_,pending_)){error(Fault::Mailbox);due_=now+20;return;}
    if(attempts_)++counts.retransmitted;
    else if(pending_.op==Op::Toggle)++counts.toggles;
    ++attempts_;due_=now+2000;
}
bool Device::receive(const Outcome& o,uint32_t generation,int64_t now){
    if(!live_ || generation!=generation_ || std::strcmp(o.session,pending_.session)){error(Fault::Session);return false;}
    const bool establishing=!baseline();
    if(establishing){
        if(!awaiting_ || pending_.op!=Op::Hello || o.seq!=pending_.seq){error(Fault::Baseline);return false;}
        if(o.state)std::strcpy(conn_,o.conn);
    }else if(std::strcmp(conn_,o.conn)){error(Fault::Connection);return false;}
    const bool changed=o.state && (establishing || o.rev>rev_);
    if(changed){rev_=o.rev;character_=o.character;}
    else if(o.state && o.rev<rev_)error(Fault::Stale);
    if(!o.state){
        const auto f=!std::strcmp(o.reason,"switch-failed")?Fault::SwitchFailed:!std::strcmp(o.reason,"mismatch")?Fault::Mismatch:!std::strcmp(o.reason,"stale")?Fault::ReplayStale:
            !std::strcmp(o.reason,"voice")?Fault::Voice:!std::strcmp(o.reason,"deadline")?Fault::Deadline:
            !std::strcmp(o.reason,"cancelled")?Fault::Cancelled:Fault::Other;
        error(f);
    }
    if(awaiting_ && o.seq==pending_.seq){awaiting_=false;++counts.answered;
        if(pending_.op==Op::Hello){
            if(o.state){hello_errors_=0;hello_retry_=false;}
            else if(hello_errors_<3){
                // A cached hello error cannot recover by replaying its seq.
                // Keep tap admission pending while a new-seq hello backs off.
                due_=now+(1000ll<<hello_errors_++);hello_retry_=true;
            }else waiting_connect_=true;
        }
    }
    return changed;
}
}
