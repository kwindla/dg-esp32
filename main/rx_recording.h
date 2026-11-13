#pragma once
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <mutex>
namespace dg {
// The received-audio diagnostic recording. Playback appends, a new session or
// `rx-clear` resets, and the console's `dump-rx` exports it over many paced
// serial lines. An export is bound to the recording it started on: once that
// recording has been reset, its reads fail, so a dump is one recording or an
// explicit refusal, never a mixture. The lock is held per append and per
// chunk copy, never across serial output.
class RxRecording {
public:
    struct Snapshot {uint32_t generation;size_t samples;};
    RxRecording(int16_t* storage,size_t capacity)noexcept:storage_(storage),capacity_(capacity){}
    void append(const int16_t* pcm,size_t n) {
        std::lock_guard<std::mutex> lock(mutex_);
        if(samples_+n<=capacity_){std::memcpy(storage_+samples_,pcm,n*sizeof(int16_t));samples_+=n;published_=samples_;}
    }
    void reset() {std::lock_guard<std::mutex> lock(mutex_);samples_=0;published_=0;++generation_;}
    size_t samples()const noexcept{return published_.load();}
    Snapshot snapshot()const{std::lock_guard<std::mutex> lock(mutex_);return {generation_,samples_};}
    // Copies samples [at, at+n) of the snapshot's recording.
    bool read(const Snapshot& from,size_t at,int16_t* out,size_t n)const {
        std::lock_guard<std::mutex> lock(mutex_);
        if(from.generation!=generation_ || at>from.samples || n>from.samples-at)return false;
        std::memcpy(out,storage_+at,n*sizeof(int16_t));return true;
    }
private:
    mutable std::mutex mutex_;
    int16_t* storage_;size_t capacity_,samples_=0;uint32_t generation_=0;
    std::atomic<size_t> published_{0};
};
}
