#include "dg_audio/aec_config.h"
#include "sdkconfig.h"
#include "dg_audio/aec.h"
#include "dg_audio/reference_gain.h"
#if CONFIG_DG_ECHOEAR_AEC
#if !CONFIG_HEAP_ABORT_WHEN_ALLOCATION_FAILS
#error "AEC requires heap fail-stop before unchecked vendor allocation results are used"
#endif
#include "dg_audio/aec_framer.h"
#include "codec/es7210_slots.h"
#include "dg_board/audio_hw.h"
#include "dg_audio/character_signals.h"
#include "esp_afe_aec.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include <atomic>
#include <cstdlib>
#include <cstdio>
#include <new>
#include <mutex>

namespace dg_audio {
namespace {
constexpr unsigned internal_caps=MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT;
constexpr unsigned psram_caps=MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT;
// A single board-lifetime microphone owns the canceller. Destruction happens
// after the capture worker joins and after console/stat consumers stop.
std::atomic<void*> active{nullptr};
std::atomic<unsigned> selected_reference_gain{0};
int16_t* buffer(size_t samples) {
    auto* p=static_cast<int16_t*>(heap_caps_aligned_calloc(16,samples,sizeof(int16_t),psram_caps));
    if(!p)throw std::bad_alloc();
    return p;
}
}
struct EchoCanceller::Impl {
    afe_aec_handle_t* handle=nullptr;
    int16_t *input=nullptr,*processed=nullptr,*fifo=nullptr;
    AecFramer* framer=nullptr;
    unsigned chunk=0;
    int internal_bytes=0,psram_bytes=0;
    std::atomic<uint64_t> calls{0},wall_us{0},input_samples{0},output_samples{0};
    std::atomic<uint64_t> errors{0},reference_clips{0},output_clips{0};
    std::atomic<unsigned> max_us{0};
    std::array<std::atomic<uint32_t>,8> duration_buckets{};
    std::atomic<uint64_t> timing_rows{0},timing_row_us{0};
    std::atomic<unsigned> timing_row_max_us{0};
    std::atomic<uint64_t> reference_low_samples{0},reference_low_windows{0};
    std::atomic<unsigned> reference_low_longest_samples{0};
    unsigned reference_low_run=0,reference_gain_db=0;

    std::mutex capture_mutex;
    static constexpr size_t capture_frames=30*16000;
    int16_t* capture=nullptr;
    static constexpr size_t timing_capacity=3000;
    AecCaptureTiming* timing=nullptr;
    size_t timing_count=0;
    size_t captured=0;
    unsigned capture_channels=3,capture_capacity_channels=0;
    bool recording=false,truncated=false;
    Impl() {
        const auto before_internal=heap_caps_get_free_size(internal_caps);
        const auto before_psram=heap_caps_get_free_size(psram_caps);
        // Public AFE AEC-only interface: one mic + physical loopback. No NS,
        // AGC, VAD/Wake models, extra worker or capture gain manipulation.
        // Pinned 2.4.7's AEC allocator explicitly requests SPIRAM|8BIT.
        handle=afe_aec_create("MR",4,AFE_TYPE_FD,AFE_MODE_LOW_COST);
        // The vendor destructor assumes a complete nested engine. Never unwind
        // a rejected partial factory result through release()/afe_aec_destroy().
        if(!handle || !handle->handle || !handle->handle->aec_handle || !handle->data ||
           !handle->pcm_config.mic_ids || !handle->pcm_config.ref_ids) {
            std::fputs("Fatal incomplete AFE engine\n",stderr);std::abort();
        }
        try {
            const auto& pcm=handle->pcm_config;
            if(pcm.total_ch_num!=2 || pcm.mic_num!=1 || pcm.ref_num!=1 || pcm.sample_rate!=16000 ||
               pcm.mic_ids[0]!=0 || pcm.ref_ids[0]!=1 || handle->mode!=AEC_MODE_FD_LOW_COST)
                throw std::runtime_error("Invalid AFE MR configuration");
            chunk=afe_aec_get_chunksize(handle);
            if(!chunk || chunk>2048)throw std::runtime_error("Unsupported AFE AEC chunk size");
            input=buffer(chunk*2);processed=buffer(chunk);fifo=buffer(AecFramer::fifo_samples(chunk));
            auto* p=heap_caps_malloc(sizeof(AecFramer),psram_caps);if(!p)throw std::bad_alloc();
            framer=new(p) AecFramer(chunk,input,processed,fifo,[](void* p,const int16_t* in,int16_t* out) {
                auto& self=*static_cast<Impl*>(p);auto begin=esp_timer_get_time();
                auto bytes=afe_aec_process(self.handle,in,out);
                const unsigned elapsed=esp_timer_get_time()-begin;
                ++self.calls;self.wall_us+=elapsed;self.max_us=std::max(self.max_us.load(),elapsed);
                constexpr unsigned bounds[]={1000,2000,4000,8000,12000,16000,24000};
                size_t bucket=0;while(bucket<7 && elapsed>bounds[bucket])++bucket;
                self.duration_buckets[bucket].fetch_add(1,std::memory_order_relaxed);
                return bytes;
            },this);
        } catch(...) {release();throw;}
        internal_bytes=int(before_internal)-int(heap_caps_get_free_size(internal_caps));
        psram_bytes=int(before_psram)-int(heap_caps_get_free_size(psram_caps));
        dg_board::audio_hw(true).log_aec_init(chunk,chunk,internal_bytes,psram_bytes);
    }
    void release() {
        if(framer) {framer->~AecFramer();free(framer);framer=nullptr;}
        free(input);free(processed);free(fifo);input=processed=fifo=nullptr;
        free(capture);capture=nullptr;free(timing);timing=nullptr;
        if(handle) {afe_aec_destroy(handle);handle=nullptr;}
    }
    ~Impl() {release();}
    AecStatus status() const {
        AecStatus result{true,chunk,chunk,internal_bytes,psram_bytes,calls.load(),wall_us.load(),
            input_samples.load(),output_samples.load(),errors.load(),reference_clips.load(),
            output_clips.load(),max_us.load()};
        for(size_t i=0;i<duration_buckets.size();++i)result.duration_buckets[i]=duration_buckets[i].load();
        result.timing_rows=timing_rows;result.timing_row_us=timing_row_us;result.timing_row_max_us=timing_row_max_us;
        result.reference_low_samples=reference_low_samples;result.reference_low_windows=reference_low_windows;
        result.reference_low_longest_samples=reference_low_longest_samples;
        result.reference_gain_db=selected_reference_gain.load();
        return result;
    }
};
void aec_reference_gain(unsigned db) {
    if(!valid_reference_gain(db))throw std::invalid_argument("Invalid reference gain");
    selected_reference_gain=db;
}
EchoCanceller::EchoCanceller() {
    auto* p=heap_caps_malloc(sizeof(Impl),psram_caps);if(!p)throw std::bad_alloc();
    try {impl_=new(p) Impl;}catch(...) {free(p);throw;}
    active=impl_;
}
EchoCanceller::~EchoCanceller() {active=nullptr;if(impl_) {impl_->~Impl();free(impl_);}}
bool EchoCanceller::process(const int16_t* mic,const int16_t* ref,size_t n,int16_t* out,const uint32_t* stereo32,AecReadTiming timing) {
    auto& s=*impl_;
    if(!mic || !ref || !out || n>320) {++s.errors;return false;}
    for(size_t i=0;i<n;++i)if(ref[i]==32767 || ref[i]==-32768)++s.reference_clips;
    uint64_t ref_power=0;for(size_t i=0;i<n;++i)ref_power+=int32_t(ref[i])*ref[i];
    // Both conservative capture bounds must be audible; do not count onset,
    // offset or gaps from a single midpoint estimate. Normalize by MIC3 gain.
    const auto gain=selected_reference_gain.load();
    if(gain!=s.reference_gain_db) {s.reference_low_run=0;s.reference_gain_db=gain;}
    const auto lower=character_signals.read(uint32_t(timing.lower_us/1000)).speaker_power;
    const auto upper=character_signals.read(uint32_t(timing.upper_us/1000)).speaker_power;
    const bool audible=lower>AecStatus::playback_power_threshold && upper>AecStatus::playback_power_threshold;
    if(n && !timing.gap && timing.lower_us>=0 && timing.upper_us>=timing.lower_us && audible &&
       reference_power_low(ref_power,n,std::min(lower,upper),gain)) {
        s.reference_low_samples+=n;
        const auto before=s.reference_low_run;s.reference_low_run+=n;
        if(before<AecStatus::reference_window_samples && s.reference_low_run>=AecStatus::reference_window_samples)
            ++s.reference_low_windows;
        s.reference_low_longest_samples=std::max(s.reference_low_longest_samples.load(),s.reference_low_run);
    } else s.reference_low_run=0;
    bool success=s.framer->run(mic,ref,n,out);
    if(!success) {++s.errors;std::fill_n(out,n,0);}
    for(size_t i=0;i<n;++i)if(out[i]==32767 || out[i]==-32768)++s.output_clips;
    s.input_samples=s.framer->input_samples();s.output_samples=s.framer->output_samples();
    {
        std::lock_guard<std::mutex> lock(s.capture_mutex);
        if(s.recording) {
            if(s.capture_channels==5 && n && !stereo32) {
                ++s.errors;std::fill_n(out,n,0);return false; // Never fabricate raw channels.
            }
            const auto count=std::min(n,Impl::capture_frames-s.captured);
            const auto& hw=dg_board::audio_hw(true);
            for(size_t i=0;i<count;++i) {
                auto* frame=s.capture+s.capture_channels*(s.captured+i);
                if(s.capture_channels==5) {
                    const auto raw=es7210_slots(stereo32[2*i],stereo32[2*i+1],hw);
                    frame[0]=raw.mic1;frame[1]=raw.mic2;frame[2]=raw.mic3;frame[3]=raw.mic4;frame[4]=out[i];
                } else {frame[0]=mic[i];frame[1]=ref[i];frame[2]=out[i];}
            }
            if(count) {
                const auto row_begin=esp_timer_get_time();
                if(s.timing_count<Impl::timing_capacity)
                    s.timing[s.timing_count++]={s.captured,s.captured+count,timing};
                else s.truncated=true;
                const unsigned row_us=esp_timer_get_time()-row_begin;
                ++s.timing_rows;s.timing_row_us+=row_us;
                s.timing_row_max_us=std::max(s.timing_row_max_us.load(),row_us);
            }
            s.captured+=count;
            if(count!=n)s.truncated=true;
        }
    }
    return success;
}
AecStatus aec_status() {
    auto* p=static_cast<EchoCanceller::Impl*>(active.load());return p?p->status():AecStatus{};
}
void aec_record(bool enabled,bool all_slots) {
    auto* s=static_cast<EchoCanceller::Impl*>(active.load());
    if(!s)throw std::runtime_error("AEC is inactive");
    // Allocation precedes locking: capture must never wait behind heap work.
    const unsigned channels=all_slots?5:3;
    int16_t* next=nullptr;int16_t* previous=nullptr;
    if(enabled && channels>s->capture_capacity_channels)next=buffer(channels*EchoCanceller::Impl::capture_frames);
    if(enabled && !s->timing) {
        s->timing=static_cast<AecCaptureTiming*>(heap_caps_aligned_calloc(16,
            EchoCanceller::Impl::timing_capacity,sizeof(AecCaptureTiming),psram_caps));
        if(!s->timing) {free(next);throw std::bad_alloc();}
    }
    {
        std::lock_guard<std::mutex> lock(s->capture_mutex);
        if(next) {previous=s->capture;s->capture=next;s->capture_capacity_channels=channels;}
        if(enabled) {s->captured=0;s->timing_count=0;s->truncated=false;s->capture_channels=channels;}
        s->recording=enabled;
    }
    free(previous); // Allocation AND deallocation stay outside the capture lock.
}
AecCapture aec_capture() {
    auto* s=static_cast<EchoCanceller::Impl*>(active.load());
    if(!s)throw std::runtime_error("AEC is inactive");
    std::lock_guard<std::mutex> lock(s->capture_mutex);
    if(s->recording)throw std::runtime_error("Stop AEC capture before dump");
    return {s->capture,s->captured,s->truncated,s->capture_channels,s->timing,s->timing_count};
}
}
#else
namespace dg_audio { AecStatus aec_status() {return {};} }
#endif
