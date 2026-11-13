#pragma once
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <stdexcept>

namespace dg_audio {
// Adapt the library's queried frame size to the unchanged <=320-sample IO.
// All storage belongs to the caller (PSRAM on the device). A single, explicit
// frame of initial silence provides a constant sample delay, with no repeated
// padding, frame removal or turn/session reset. The processor itself may add
// latency, which acoustic qualification must measure independently.
class AecFramer {
public:
    using Process = size_t (*)(void*, const int16_t*, int16_t*);
    AecFramer(size_t chunk, int16_t* input, int16_t* processed, int16_t* fifo,
              Process process, void* context)
        : chunk_(chunk), input_(input), processed_(processed), fifo_(fifo),
          process_(process), context_(context), available_(chunk) {
        if(!chunk || chunk>2048 || !input || !processed || !fifo || !process)
            throw std::invalid_argument("Invalid AFE frame adapter");
        std::fill_n(fifo_, chunk_, 0);
    }
    static size_t fifo_samples(size_t chunk) { return 2*chunk+320; }
    bool run(const int16_t* microphone, const int16_t* reference, size_t n, int16_t* output) {
        if(!microphone || !reference || !output || n>320) {++errors_;return false;}
        bool success=true;
        for(size_t i=0;i<n;++i) {
            input_[2*filled_]=microphone[i];input_[2*filled_+1]=reference[i];
            if(++filled_!=chunk_)continue;
            const auto bytes=process_(context_,input_,processed_);++calls_;filled_=0;
            if(bytes!=chunk_*sizeof(int16_t)) {
                ++errors_;success=false;std::fill_n(processed_,chunk_,0);
            }
            if(available_+chunk_>fifo_samples(chunk_)) {++errors_;return false;}
            for(size_t j=0;j<chunk_;++j) {
                fifo_[(head_+available_)%fifo_samples(chunk_)]=processed_[j];++available_;
            }
        }
        if(available_<n) {++errors_;return false;}
        for(size_t i=0;i<n;++i) {
            output[i]=fifo_[head_];head_=(head_+1)%fifo_samples(chunk_);--available_;
        }
        input_samples_+=n;output_samples_+=n;
        return success;
    }
    uint64_t errors() const {return errors_;}
    uint64_t calls() const {return calls_;}
    uint64_t input_samples() const {return input_samples_;}
    uint64_t output_samples() const {return output_samples_;}
private:
    size_t chunk_,filled_=0,head_=0;
    int16_t *input_,*processed_,*fifo_;
    Process process_;void* context_;size_t available_;
    uint64_t errors_=0,calls_=0,input_samples_=0,output_samples_=0;
};
}
