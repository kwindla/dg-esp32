#pragma once
#include <array>
#include <deque>
#include <algorithm>
#include <cstdint>

// ESP-IDF 6.1 TX DMA model: 4x240 frames, 3-deep EOF queue. A write
// abandons curr_ptr even when partial if <=1 queue slot remains. EOF
// auto-clears the descriptor and drops the oldest queue entry on overflow.
struct TxRing {
    static constexpr unsigned frames=240, descriptors=4;
    std::deque<unsigned> free;
    int current=-1;
    unsigned offset=0;
    uint64_t completed=0;
    unsigned abandoned=0, overflows=0;
    void reset() {free.clear();current=-1;offset=0;completed=0;abandoned=overflows=0;}
    unsigned preload(unsigned n) {
        if(current<0) {current=0;offset=0;free={1,2,3};}
        unsigned done=0;
        while(n) {
            if(offset==frames) {if(free.empty())break;current=free.front();free.pop_front();offset=0;}
            auto count=std::min(n,frames-offset);offset+=count;done+=count;n-=count;
        }
        return done;
    }
    void complete() {
        unsigned descriptor=(completed/frames)%descriptors;completed+=frames;
        if(free.size()==3) {free.pop_front();++overflows;}
        free.push_back(descriptor);
    }
    // One non-blocking driver iteration; the caller models the wait/timeout.
    unsigned write(unsigned n) {
        if(current<0 || offset==frames || free.size()>=2) {
            if(free.empty())return 0;
            if(current>=0 && offset<frames)abandoned+=frames-offset;
            current=free.front();free.pop_front();offset=0;
        }
        auto count=std::min(n,frames-offset);offset+=count;return count;
    }
    // Physical sample address of the next write (after the early-pop rule).
    uint64_t write_position() const {
        if(current<0 || offset==frames || free.size()>=2) {
            if(free.empty())return UINT64_MAX;
            unsigned descriptor=free.front();
            auto cycle=completed/(frames*descriptors);
            auto address=cycle*frames*descriptors+descriptor*frames;
            if(address<completed)address+=frames*descriptors;
            return address;
        }
        auto address=completed/(frames*descriptors)*frames*descriptors+unsigned(current)*frames;
        if(address+frames<=completed)address+=frames*descriptors;
        return address+offset;
    }
};
