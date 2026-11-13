#include "playout.h"
#include <algorithm>
#include <cstring>
namespace pipecat::esp_peer {
Playout::Playout(Storage& storage):storage_(storage) {}
void Playout::drift_reset() {
    filter_at_=filter_count_=filter_sum_=0;drift_sign_=0;persistence_us_=0;
}
void Playout::end_missing_run() {
    if(missing_run_)++counts_.missing_run_ended[std::min(missing_run_,6u)-1];
    missing_run_=0;
}
void Playout::invalidate() {
    ++generation_;
    for(auto& s:slots_)if(s.owner==Ownership::Ready)s.owner=Ownership::Free;
    // Outstanding copies/leases release themselves; generations prohibit reuse.
    candidate_=-1;end_missing_run();drift_reset();reset_decoder_=true;
}
void Playout::authorize(uint32_t ssrc,uint32_t track) {
    std::lock_guard<std::mutex> lock(mutex_);
    if(track!=track_generation_) {allowed_ssrc_=ssrc;track_generation_=track;}
}
void Playout::start(int64_t ts,int64_t seq,uint32_t ssrc,int64_t now,uint8_t reason) {
    cancel_latched_=false; // A new RTP anchor can precede BotStarted.
    reset_retired_=0;for(const auto& s:slots_)reset_retired_+=s.owner==Ownership::Ready;
    previous_watermark_=watermark_;reset_reason_=reason;
    invalidate();anchor_=watermark_=high_ts_=ts;high_seq_=seq;
    startup_floor_=ts-target_frames*frame_ticks;
    active_ssrc_=ssrc;active_track_generation_=track_generation_;state_=State::Prefill;started_=false;
    prefill_us_=now+int64_t(target_frames)*20000;last_decode_us_=now;++counts_.prefills;
}
int Playout::find(int64_t ts) const {
    for(unsigned i=0;i<capacity;++i) {
        const auto& s=slots_[i];
        if(s.generation==generation_ && s.ts==ts && s.owner==Ownership::Ready && int(i)!=candidate_)return i;
    }
    return -1;
}
unsigned Playout::contiguous(int64_t ts) const {
    unsigned n=0;while(n<capacity && find(ts+n*frame_ticks)>=0)++n;return n;
}
unsigned Playout::useful() const {
    unsigned n=0;for(unsigned i=0;i<capacity;++i) {
        const auto& s=slots_[i];
        n+=s.owner==Ownership::Ready && s.generation==generation_ && int(i)!=candidate_ && s.ts>=watermark_;
    }
    return n;
}
void Playout::reject(IngressError reason) {
    std::lock_guard<std::mutex> lock(mutex_);++counts_.ingress;++counts_.rejected[unsigned(reason)];
}
Playout::Token Playout::reserve(const RtpAudioView& p,int64_t now,const dg::DeviceProgress& device) {
    std::lock_guard<std::mutex> lock(mutex_);++counts_.ingress;
    auto reject=[&](IngressError reason) {++counts_.rejected[unsigned(reason)];return Token{};};
    if(state_==State::Disconnected)return reject(IngressError::Generation);
    if(!p.payload || !p.size || p.size>1500)return reject(IngressError::Malformed);
    if(!track_generation_ || p.ssrc!=allowed_ssrc_)return reject(IngressError::Source);
    if(now<mute_until_us_) {++counts_.cancelled_packets;return {};}
    if(state_==State::Empty || p.ssrc!=active_ssrc_ || active_track_generation_!=track_generation_) {
        // Explicit accepted signaling generation is the only source authority.
        if(state_!=State::Empty)++counts_.discontinuity;
        start(p.timestamp,p.sequence,p.ssrc,now,state_==State::Empty?0:1);
    }
    // Device completion is authoritative even while the playback task is
    // parked. Retire expired intervals before testing the 32-frame window.
    if(state_==State::Playing && tick_seen_ && device.valid && device.epoch==device_epoch_ &&
       device.expired_ticks>last_tick_+1)
        retire_device(unsigned(device.expired_ticks-last_tick_-1));
    int64_t seq=high_seq_+int16_t(p.sequence-uint16_t(high_seq_));
    int64_t ts=high_ts_+int32_t(p.timestamp-uint32_t(high_ts_));
    for(unsigned i=0;i<capacity;++i) {
        auto& s=slots_[i];
        if(s.generation!=generation_ || s.owner==Ownership::Free || s.seq!=seq)continue;
        if(int(i)==candidate_ && s.owner==Ownership::Ready && s.ts!=ts) {
            s.owner=Ownership::Free;candidate_=-1;++counts_.rejected[unsigned(IngressError::Conflict)];
            continue;
        }
        return reject(s.ts==ts?IngressError::Duplicate:IngressError::Conflict);
    }
    const auto advance=seq-high_seq_,delta=ts-high_ts_;
    // Consecutive sequence holes inside the address window are rendered as
    // missing media, even when their timestamp residual exceeds 160 ms.
    const bool adjacent_gap=advance==1 && delta>frame_ticks && delta%frame_ticks==0;
    const bool in_window=ts>=watermark_ && ts<watermark_+capacity*frame_ticks;
    const bool confirmer=candidate_>=0 && slots_[candidate_].owner==Ownership::Ready &&
        seq==slots_[candidate_].seq+1 && ts==slots_[candidate_].ts+frame_ticks;
    const bool candidate_needed=advance>0 && ((ts-anchor_)%frame_ticks || delta<0 ||
        (adjacent_gap?!in_window:std::abs(delta-advance*frame_ticks)>8*frame_ticks));
    const bool anomaly=confirmer || candidate_needed;
    bool confirmed=false;
    if(anomaly) {
        if(candidate_>=0) {
            auto& c=slots_[candidate_];
            if(c.owner==Ownership::Ready && seq==c.seq+1 && ts==c.ts+frame_ticks && seq>high_seq_) {
                start(c.ts,c.seq,c.ssrc,c.ingress_us,2);
                if(reset_retired_)--reset_retired_; // probation payload survives the reset
                // The sole probation payload remains in its existing slot.
                // invalidate changes ownership, preserving slot metadata/payload.
                c.generation=generation_;c.owner=Ownership::Ready;
                ++counts_.accepted;++counts_.discontinuity;
                high_ts_=c.ts;high_seq_=c.seq;confirmed=true;
            } else {
                // A failed confirmer does not change accepted high water.
                if(c.owner==Ownership::Ready) {c.owner=Ownership::Free;++counts_.rejected[unsigned(IngressError::Conflict)];}
                candidate_=-1;
            }
        }
        if(candidate_<0 && candidate_needed && !confirmed) {
            int free=-1;for(unsigned i=0;i<capacity;++i)if(slots_[i].owner==Ownership::Free){free=i;break;}
            if(free<0)return reject(IngressError::Capacity);
            slots_[free]={Ownership::Writing,generation_,ts,seq,now,p.ssrc,p.size,false};candidate_=free;
            return {free,generation_,true};
        }
    } else if(candidate_>=0 && advance>0) {
        auto& c=slots_[candidate_];
        if(c.owner==Ownership::Ready) {c.owner=Ownership::Free;++counts_.rejected[unsigned(IngressError::Conflict)];}
        candidate_=-1;
    }
    if((ts-anchor_)%frame_ticks)return reject(IngressError::Conflict);
    // Admission is transactional: rejecting a reservation cannot move W.
    int64_t proposed=watermark_;
    if(ts<watermark_) {
        if(state_==State::Prefill && !started_ && ts>=startup_floor_)proposed=ts;
        else return reject(IngressError::Late);
    }
    if(state_==State::Starved)proposed=ts;
    if(ts>=proposed+capacity*frame_ticks)return reject(IngressError::Window);
    for(const auto& s:slots_) {
        if(s.generation!=generation_ || s.owner==Ownership::Free)continue;
        if(s.ts==ts)return reject(IngressError::Duplicate);
        if(proposed<watermark_ && s.ts>=proposed+capacity*frame_ticks)
            return reject(IngressError::Window);
    }
    // Small inconsistent backwards sequences conflict. Large same-source
    // restarts may confirm a rebase above and receive the fixed prefill.
    if(seq<high_seq_ && ts>high_ts_)return reject(IngressError::Conflict);
    int free=-1;for(unsigned i=0;i<capacity;++i)if(slots_[i].owner==Ownership::Free){free=i;break;}
    if(free<0)return reject(IngressError::Capacity);
    if(state_==State::Starved)start(ts,seq,p.ssrc,now,3);
    else watermark_=proposed;
    slots_[free]={Ownership::Writing,generation_,ts,seq,now,p.ssrc,p.size,false};
    return {free,generation_,false};
}
bool Playout::publish(Token token,const uint8_t* payload,size_t size) {
    if(!token)return false;
    // The WRITING slot is exclusively ours even across reset/disconnect.
    if(payload && size<=1500)std::memcpy(storage_[token.slot].data,payload,size);
    std::lock_guard<std::mutex> lock(mutex_);auto& s=slots_[token.slot];
    if(s.owner!=Ownership::Writing || s.generation!=token.generation)return false;
    auto reject=[&](IngressError reason) {s.owner=Ownership::Free;if(candidate_==token.slot)candidate_=-1;
        ++counts_.rejected[unsigned(reason)];return false;};
    if(s.cancelled) {s.owner=Ownership::Free;return false;}
    if(generation_!=token.generation || state_==State::Disconnected)return reject(IngressError::Generation);
    if(!payload || size!=s.size)return reject(IngressError::Malformed);
    if(!token.candidate && s.ts<watermark_)return reject(IngressError::Late);
    s.owner=Ownership::Ready;
    if(token.candidate && candidate_!=token.slot)return reject(IngressError::Generation);
    if(token.candidate)return true; // probation isn't accepted/future media yet
    ++counts_.accepted;
    // Each accepted adjacency is considered once, when the later member
    // arrives or the earlier reordered member fills it. Slots retain markers.
    for(auto& later:slots_)if(later.generation==generation_ && later.owner!=Ownership::Free &&
        later.owner!=Ownership::Writing && later.seq==s.seq+1 && later.ts>s.ts+frame_ticks && !later.gap_before) {
        later.gap_before=true;++counts_.ts_gap;drift_reset();
    }
    if(s.seq==high_seq_+1 && s.ts>high_ts_+frame_ticks && !s.gap_before) {
        s.gap_before=true;++counts_.ts_gap;drift_reset();
    }
    if(s.seq>high_seq_) {high_seq_=s.seq;high_ts_=s.ts;}
    ready_high_water_=std::max(ready_high_water_,contiguous(watermark_));return true;
}
bool Playout::insert(const RtpAudioView& p,int64_t now,const dg::DeviceProgress& device) {return publish(reserve(p,now,device),p.payload,p.size);}
void Playout::release(Token token) {
    if(!token)return;
    std::lock_guard<std::mutex> lock(mutex_);auto& s=slots_[token.slot];
    if(s.generation==token.generation && s.owner==Ownership::Reading)s.owner=Ownership::Free;
}
void Playout::retire(unsigned n) {
    for(unsigned i=0;i<capacity;++i) {
        auto& s=slots_[i];
        if(s.generation==generation_ && s.owner==Ownership::Ready && s.ts>=watermark_ &&
            s.ts<watermark_+int64_t(n)*frame_ticks && int(i)!=candidate_)s.owner=Ownership::Free;
    }
    watermark_+=int64_t(n)*frame_ticks;counts_.media_advance+=n;
}
void Playout::retire_device(unsigned total) {
    if(total<=ingress_retired_)return;
    unsigned n=total-ingress_retired_;
    retire(n);counts_.device_silence_blocks+=n;ingress_retired_=total;
    drift_reset();end_missing_run();
}
unsigned Playout::drift(unsigned remaining,int64_t now,bool eligible,Decision& decision) {
    if(!eligible) {drift_reset();return 0;}
    if(filter_count_==50)filter_sum_-=occupancy_[filter_at_];else ++filter_count_;
    occupancy_[filter_at_]=remaining;filter_sum_+=remaining;filter_at_=(filter_at_+1)%50;
    decision.drift_filter_sum=filter_sum_;decision.drift_filter_count=filter_count_;
    if(filter_count_<50)return 0;
    int sign=filter_sum_>(target_frames+1)*50?1:filter_sum_<(target_frames-1)*50?-1:0;
    if(sign!=drift_sign_) {drift_sign_=sign;persistence_us_=now;}
    if(!sign || now-persistence_us_<1000000 || now-correction_us_<1000000)return 0;
    decision.drift_persistence_us=now-persistence_us_;
    correction_us_=now;drift_sign_=0;persistence_us_=now;
    return sign>0?1:2;
}
Playout::Decision Playout::pull(const dg::RenderTick& tick,bool /*prior*/) {
    std::lock_guard<std::mutex> lock(mutex_);Decision d;
    d.generation=generation_;d.render_us=tick.deadline_us;d.ssrc=active_ssrc_;
    if(state_==State::Disconnected) {d.kind=Kind::Disconnected;return d;}
    if(tick_seen_ && tick.device_epoch<device_epoch_)return d;
    if(!tick.valid || (tick_seen_ && tick.device_epoch==device_epoch_ && tick.index<=last_tick_))return d;
    unsigned skipped=tick.silence_blocks;
    if(tick_seen_ && tick.device_epoch==device_epoch_) {
        skipped=unsigned(tick.index-last_tick_-1);
        if(skipped!=tick.silence_blocks)++counts_.tick_resync;
    }
    if(state_==State::Playing)retire_device(skipped);
    ingress_retired_=0;
    device_epoch_=tick.device_epoch;last_tick_=tick.index;tick_seen_=true;
    d.timestamp=watermark_;d.reset_decoder=reset_decoder_;reset_decoder_=false;
    d.reset_reason=reset_reason_;d.reset_retired=reset_retired_;d.previous_watermark=previous_watermark_;
    if(tick.deadline_us<mute_until_us_) {
        counts_.cancelled_render_intervals+=skipped+1;d.kind=Kind::Cancelled;return d;
    }
    mute_until_us_=0;
    if(state_==State::Empty) {d.kind=Kind::Idle;return d;}
    if(state_==State::Starved) {d.kind=Kind::Prefill;++counts_.render_decisions;return d;}
    ++counts_.render_decisions;
    if(state_==State::Prefill) {
        drift_reset();
        // Eight contiguous frames already provide the target seven-frame (140ms)
        // reserve after the first decode. Waiting another wall-clock 140ms
        // from the first arrival double-counts a sender/network startup batch
        // and creates a static excess that the drift controller later drops.
        // Preserve the original deadline fallback when reserve fills slowly.
        if(tick.deadline_us<prefill_us_ && contiguous(watermark_)<target_frames+1) {
            d.kind=Kind::Prefill;return d;
        }
        state_=State::Playing;started_=true;last_decode_us_=tick.deadline_us;
    }
    unsigned ready=contiguous(watermark_);
    if(ready>target_frames+3) {d.trimmed=ready-target_frames;retire(d.trimmed);counts_.latency_trim+=d.trimmed;
        drift_reset();ready=target_frames;}
    d.ready_depth=ready;
    int index=find(watermark_);
    if(index>=0) {
        unsigned edit=drift(ready?ready-1:0,tick.deadline_us,missing_run_==0 && !slots_[index].gap_before,d);
        if(edit==2) {d.kind=Kind::Insert;++counts_.drift_insert;d.timestamp=watermark_;return d;}
        if(edit==1 && find(watermark_+frame_ticks)>=0) {
            retire(1);++counts_.drift_drop;d.skipped=1;index=find(watermark_);
        }
        auto& s=slots_[index];s.owner=Ownership::Reading;
        d.kind=Kind::Decode;d.lease={index,generation_,false};d.payload=storage_[index].data;
        d.size=s.size;d.timestamp=watermark_;
        watermark_+=frame_ticks;++counts_.media_advance;
        return d;
    }
    drift_reset();
    if(!skipped && tick.deadline_us-last_decode_us_>=500000 && !useful()) {
        state_=State::Starved;++counts_.starvation;d.kind=Kind::Prefill;return d;
    }
    ++counts_.missing_slots;++missing_run_;d.missing_run=missing_run_;
    d.kind=missing_run_<=5?Kind::Plc:Kind::Zero;
    if(d.kind==Kind::Plc)++counts_.plc_calls;else ++counts_.post_plc_zeros;
    retire(1);return d;
}
void Playout::finish(const Decision& d,bool decoded) {
    std::lock_guard<std::mutex> lock(mutex_);
    if(d.generation==generation_ && d.kind==Kind::Decode) {
        if(decoded) {end_missing_run();++counts_.decoded_packets;last_decode_us_=std::max(last_decode_us_,d.render_us);}
        else {++counts_.decode_errors;drift_reset();}
    }
    if(d.lease && slots_[d.lease.slot].owner==Ownership::Reading && slots_[d.lease.slot].generation==d.lease.generation)
        slots_[d.lease.slot].owner=Ownership::Free;
}
bool Playout::cancel(int64_t now) {
    std::lock_guard<std::mutex> lock(mutex_);
    if(state_==State::Disconnected || cancel_latched_)return false;
    cancel_latched_=true;++counts_.cancellations;
    for(auto& s:slots_)if(s.owner!=Ownership::Free && s.generation==generation_) {
        ++counts_.cancelled_packets;s.cancelled=true;
    }
    invalidate();state_=State::Empty;started_=false;ingress_retired_=0;
    reset_retired_=0;reset_reason_=4;previous_watermark_=watermark_;
    mute_until_us_=now+150000;return true;
}
void Playout::resume() {
    std::lock_guard<std::mutex> lock(mutex_);mute_until_us_=0;cancel_latched_=false;
}
void Playout::disconnect() {std::lock_guard<std::mutex> lock(mutex_);if(state_!=State::Disconnected)invalidate();state_=State::Disconnected;mute_until_us_=0;}
bool Playout::ready() const {std::lock_guard<std::mutex> lock(mutex_);return mute_until_us_!=0 || state_==State::Prefill || state_==State::Playing;}
uint64_t Playout::generation() const {std::lock_guard<std::mutex> lock(mutex_);return generation_;}
Playout::Snapshot Playout::snapshot(int64_t now) const {
    std::lock_guard<std::mutex> lock(mutex_);unsigned occupied=0,span=0;int64_t age=0;
    for(unsigned i=0;i<capacity;++i) {
        const auto& s=slots_[i];occupied+=s.owner!=Ownership::Free;
        if(s.generation==generation_ && s.owner==Ownership::Ready && int(i)!=candidate_ && s.ts>=watermark_) {
            span=std::max(span,unsigned((s.ts-watermark_)/frame_ticks+1));age=std::max(age,now-s.ingress_us);
        }
    }
    return {counts_,state_,generation_,last_tick_,watermark_,age,occupied,contiguous(watermark_),ready_high_water_,span};
}
}
