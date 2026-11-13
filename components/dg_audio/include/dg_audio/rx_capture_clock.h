#pragma once
#include <array>
#include <algorithm>
#include <cstdint>
namespace dg_audio {
// Bounds the first sample of a read using observed RX completion counts. No
// nominal sample-rate extrapolation and no ISR timestamps. The timestamp MUST
// precede loading the completion counter. A count below the preceding descriptor
// boundary proves that observation preceded every sample in this read.
class RxCaptureClock {
public:
    RxCaptureClock(unsigned frames, unsigned descriptors): frames_(frames), capacity_((descriptors+1)*frames) {}
    int64_t begin(uint32_t completions,uint32_t overflows,int64_t before_count_us,int64_t fallback) {
        observe(completions,overflows);
        // Discarded partial buffers can only make actual samples newer. Bound
        // accumulated undercount after stalls by the maximum driver residence.
        cursor_=std::max(cursor_,completed_*frames_>capacity_ ? completed_*frames_-capacity_:0);
        rows_[next_]={completed_,before_count_us,true};next_=(next_+1)%rows_.size();
        const auto preceding=cursor_/frames_;
        auto lower=fallback;
        for(const auto& row:rows_) if(row.valid && row.count<preceding) lower=std::max(lower,row.time);
        return lower;
    }
    void end(uint32_t completions,uint32_t overflows,unsigned frames_read) {
        observe(completions,overflows);cursor_+=frames_read;
    }
private:
    void observe(uint32_t completions,uint32_t overflows) {
        completed_+=uint32_t(completions-last_completed_);
        cursor_+=uint64_t(uint32_t(overflows-last_overflows_))*frames_;
        last_completed_=completions;last_overflows_=overflows;
    }
    struct Row {uint64_t count=0;int64_t time=0;bool valid=false;};
    std::array<Row,8> rows_{};
    unsigned frames_,capacity_,next_=0;
    uint32_t last_completed_=0,last_overflows_=0;
    uint64_t completed_=0,cursor_=0;
};
}
