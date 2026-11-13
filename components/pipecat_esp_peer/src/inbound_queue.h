#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string_view>
#include <stdexcept>
#include <cassert>

namespace pipecat::esp_peer {
// One owner producer and one RX consumer. Serialize metadata operations only;
// leases keep WRITING/READING payloads owned while memcpy runs outside the lock.
// A WRITING head blocks FIFO consumption, and a READING slot still uses capacity.
// HELD slots are copied and owned but not yet admitted work: the owner carries
// them over and promotes them FIFO in later passes. A HELD head blocks the
// consumer, and once any slot is HELD every later publication is HELD too.
class InboundQueue {
public:
    static constexpr size_t capacity=64;
    struct Lease {char* data=nullptr;size_t index=0,size=0;int64_t at=0;
        explicit operator bool() const {return data!=nullptr;}};
    InboundQueue(char* storage,size_t bytes,size_t limit):storage_(storage),limit_(limit) {
        if(!storage || !limit || bytes/limit<capacity)throw std::invalid_argument("Inbound storage");
    }
    Lease reserve(size_t size,int64_t at) {
        // Debug contract only (removed by NDEBUG). Native callback's nonpositive
        // and oversize checks are the production guard before reserving.
        assert(size && size<=limit_);
        if(count_==capacity)return {};
        auto index=(head_+count_)%capacity;
        slots_[index]={size,at,State::Writing};++count_;
        return {storage_+index*limit_,index,size,at};
    }
    // Returns true when the slot was carried over (HELD) rather than READY.
    bool publish(const Lease& lease,bool hold=false) {
        auto& slot=slots_.at(lease.index);
        if(!lease || slot.state!=State::Writing)throw std::logic_error("Inbound publish lease");
        hold=hold || held_;
        if(hold && !held_)held_head_=lease.index;
        // Carry-over is FIFO: a HELD publication must extend the HELD run.
        if(hold && lease.index!=(held_head_+held_)%capacity)throw std::logic_error("Inbound held order");
        if(hold)++held_;
        slot.state=hold?State::Held:State::Ready;
        return hold;
    }
    // Promote carried-over slots in FIFO order while admit(size) grants work.
    template<class Admit> unsigned promote(Admit&& admit) {
        unsigned promoted=0;
        while(held_) {
            auto& slot=slots_[held_head_];
            if(slot.state!=State::Held)throw std::logic_error("Inbound held order");
            if(!admit(slot.size))break;
            slot.state=State::Ready;held_head_=(held_head_+1)%capacity;--held_;++promoted;
        }
        return promoted;
    }
    Lease claim() {
        if(!count_ || slots_[head_].state!=State::Ready)return {};
        auto& slot=slots_[head_];slot.state=State::Reading;
        return {storage_+head_*limit_,head_,slot.size,slot.at};
    }
    void release(const Lease& lease) {
        if(!lease || lease.index!=head_ || slots_[head_].state!=State::Reading)
            throw std::logic_error("Inbound release lease");
        slots_[head_]={};head_=(head_+1)%capacity;--count_;
    }
    // Convenience operations for a caller that already has exclusive access.
    bool push(std::string_view wire,int64_t at) {
        auto lease=reserve(wire.size(),at);if(!lease)return false;
        std::memcpy(lease.data,wire.data(),wire.size());publish(lease);return true;
    }
    bool pop(char* out,size_t bytes,size_t& size,int64_t& at) {
        if(!count_ || slots_[head_].state!=State::Ready)return false;
        if(!out || bytes<slots_[head_].size)throw std::invalid_argument("Inbound output");
        auto lease=claim();std::memcpy(out,lease.data,lease.size);
        size=lease.size;at=lease.at;release(lease);return true;
    }
    size_t depth() const {return count_;}
    size_t held() const {return held_;}
private:
    enum class State {Free,Writing,Ready,Reading,Held};
    struct Slot {size_t size=0;int64_t at=0;State state=State::Free;};
    char* storage_;size_t limit_,head_=0,count_=0,held_=0,held_head_=0;
    std::array<Slot,capacity> slots_{};
};
}
