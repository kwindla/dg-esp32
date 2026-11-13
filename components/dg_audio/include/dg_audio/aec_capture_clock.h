#pragma once
#include <array>
#include <cstdint>
#include <cstddef>
namespace dg_audio {
// Translate output samples to the input block they actually represent. No wall
// clock expiry: DMA backlog or a delayed capture task cannot open the gate early.
// Fixed measured adapter + vendor delay, valid only for the pinned AFE mode.
class AecCaptureClock {
public:
    static constexpr int64_t delay_samples=1024;
    void read(size_t n,int64_t lower_us,bool gap) {
        if(!n) {valid_=false;return;}
        rows_[next_]={samples_,samples_+int64_t(n),lower_us,!gap};
        next_=(next_+1)%rows_.size();
        const auto source=samples_-delay_samples;
        valid_=false;
        for(const auto& row:rows_) if(row.valid && row.begin<=source && source<row.end) {
            lower_=row.lower;valid_=true;break;
        }
        samples_+=n;
    }
    bool valid() const {return valid_;}
    int64_t lower_us() const {return lower_;}
private:
    struct Row {int64_t begin=0,end=0,lower=0;bool valid=false;};
    std::array<Row,8> rows_{};
    size_t next_=0;
    int64_t samples_=0,lower_=0;
    bool valid_=false;
};
}
