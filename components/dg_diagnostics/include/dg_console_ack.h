#pragma once
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
namespace dg_console {
// Single console producer / single output-task consumer. The console admits
// no next command while an ACK is pending. Two slots cover an early OK and
// subsequent ERR from the same command. Bulk/log records cannot use this space.
class AckMailbox {
    struct Slot {char text[128];size_t size=0,offset=0;unsigned after=0;};
    std::array<Slot,2> slots_{};
    std::atomic<unsigned> posted_{0},retired_{0};
public:
    unsigned pending()const {const auto retired=retired_.load();return posted_.load()-retired;}
    bool post(const char* text,size_t size,unsigned after){
        const auto posted=posted_.load();
        if(!size || size>sizeof(Slot::text) || posted-retired_.load()==slots_.size())return false;
        auto& slot=slots_[posted%slots_.size()];std::memcpy(slot.text,text,size);slot.size=size;slot.offset=0;slot.after=after;
        posted_=posted+1;return true;
    }
    bool ready(unsigned retired_bulk)const {
        const auto retired=retired_.load();
        return retired!=posted_.load() && int32_t(retired_bulk-slots_[retired%slots_.size()].after)>=0;
    }
    template<class Send> bool advance(Send send){
        const auto retired=retired_.load();if(retired==posted_.load())return true;
        auto& slot=slots_[retired%slots_.size()];const auto remaining=slot.size-slot.offset;
        const int n=send(slot.text+slot.offset,remaining);
        if(n<=0 || size_t(n)>remaining)return false;
        slot.offset+=size_t(n);
        if(slot.offset==slot.size)retired_=retired+1;
        return true;
    }
};
}
