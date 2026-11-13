#include "dg_audio/aec_config.h"
#include "dg_audio/pipeline.h"
#include "dg_audio/aec.h"
#include "dg_audio/character_signals.h"
#include "dg_audio/mic.h"
#include "dg_audio/mic_protection.h"
#include "dg_audio/speaker.h"
#include "dg_audio/playback_dsp.h"
#include "dg_playback_cost.h"
#include "pthread_config.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "sdkconfig.h"
#include "dg_audio/aec_probe.h"
#include "codec/audio_codec.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <thread>
#include <mutex>
#include <cmath>
#include <cstring>
#include <cstdlib>
#include <algorithm>
#include <stdexcept>
extern "C" void dg_heap_shadow_sample(void) __attribute__((weak));
extern "C" void dg_deferred_log(const char*, ...) __attribute__((weak));
#define AUDIO_LOG(...) do {if(dg_deferred_log)dg_deferred_log(__VA_ARGS__);} while(0)
extern void mic_report(const int16_t*, float*, float*, float*);
static std::atomic<int> output_volume_db{0};
static std::atomic<unsigned> in_turn_mic_clips{0};
static std::atomic<unsigned> gate_open_mic_clips{0}, gate_closed_mic_clips{0};
static std::atomic<unsigned> hardware_underflows{0}, hardware_write_errors{0}, hardware_write_timeouts{0};
// Terminal-state wakeups and task withdrawal share a mutex, so a notification
// can never target an already terminated playback task.
static std::mutex playback_wake_mutex;
static TaskHandle_t playback_task=nullptr;
extern "C" void dg_audio_media_invalidated(void) {
    std::lock_guard<std::mutex> lock(playback_wake_mutex);
    if(playback_task)xTaskNotifyGive(playback_task);
}
extern "C" unsigned dg_audio_mic_in_turn_clips() {return in_turn_mic_clips.load();}
extern "C" unsigned dg_audio_mic_gate_open_clips() {return gate_open_mic_clips.load();}
extern "C" unsigned dg_audio_mic_gate_closed_clips() {return gate_closed_mic_clips.load();}
extern "C" int dg_audio_output_volume_db() {return output_volume_db.load();}
extern "C" unsigned dg_audio_i2s_write_errors() {return hardware_write_errors.load();}
extern "C" unsigned dg_audio_i2s_write_timeouts() {return hardware_write_timeouts.load();}
extern "C" unsigned dg_audio_i2s_underflows() {return hardware_underflows.load();}
namespace dg_audio {
static const char* TAG = "dg-audio";
static void delay() { vTaskDelay(pdMS_TO_TICKS(5)); }
static SpeechLevelConfig speech_config(int target=CONFIG_DG_PLAYBACK_TARGET_DBFS) {
    SpeechLevelConfig c;
    // The seed symbol is owned by pipecat_esp_peer/Kconfig; main pulls in
    // that component. Standalone dg_audio projects must supply that Kconfig.
    c.seed_dbfs=CONFIG_DG_PLAYBACK_SEED_DBFS_X10/10.f;
    c.target_dbfs=target;c.max_gain_db=CONFIG_DG_PLAYBACK_MAX_GAIN_DB;
    c.activity_dbfs=CONFIG_DG_PLAYBACK_ACTIVITY_DBFS;c.level_ms=CONFIG_DG_PLAYBACK_LEVEL_MS;
    c.attack_ms=CONFIG_DG_PLAYBACK_ATTACK_MS;c.release_ms=CONFIG_DG_PLAYBACK_RELEASE_MS;
    c.lookahead_ms=CONFIG_DG_PLAYBACK_LOOKAHEAD_MS;c.limiter_release_ms=CONFIG_DG_PLAYBACK_LIMITER_RELEASE_MS;
    c.compression_ratio=CONFIG_DG_PLAYBACK_COMPRESSION_RATIO;
    c.compression_threshold_dbfs=CONFIG_DG_PLAYBACK_COMPRESSION_THRESHOLD_DBFS;
    return c;
}
struct Pipeline::Impl {
    Mic mic; Speaker speaker; Gate gate;
#if CONFIG_DG_PLAYBACK_NORMALIZE
    PlaybackDsp dsp{CONFIG_DG_PLAYBACK_CEILING_PERCENT/100.0f,CONFIG_DG_PLAYBACK_HIGHPASS_HZ,speech_config()};
    int playback_target=CONFIG_DG_PLAYBACK_TARGET_DBFS;
#else
    PlaybackDsp dsp{CONFIG_DG_PLAYBACK_GAIN_PERCENT/100.0f,
                    CONFIG_DG_PLAYBACK_CEILING_PERCENT/100.0f,CONFIG_DG_PLAYBACK_HIGHPASS_HZ};
    int playback_target=0;
#endif
    std::atomic<uint64_t> limiter_samples{0}, active_samples{0}, peak_exceedances{0}, dsp_resets{0};
#if CONFIG_DG_SPEAKER_DIAGNOSTICS
    std::mutex diagnostic_mutex;
    int16_t* diagnostic_buffer=nullptr;
    size_t diagnostic_count=0;
    std::atomic<bool> diagnostic_enabled{false};
    std::mutex fixture_mutex;
    const int16_t* fixture_pcm=nullptr;
    size_t fixture_count=0, fixture_at=0;
    std::atomic<bool> fixture_enabled{false};
#endif
    std::atomic<unsigned> playback_gain{CONFIG_DG_PLAYBACK_GAIN_PERCENT};
    unsigned playback_ceiling=CONFIG_DG_PLAYBACK_CEILING_PERCENT;
    std::mutex gate_mutex, speaker_mutex;
    std::atomic<bool> stop{false}, capture_enabled{false}, silent_reports{true};
    std::atomic<unsigned> capture_epoch{0};
#if CONFIG_DG_PLAYBACK_BENCH
    std::atomic<unsigned> pause_ms{0};
#endif
    std::atomic<bool> benchmark_paused{false}, capture_parked{false}, playback_parked{false};
    std::atomic<unsigned> drops{0}, read_errors{0}, write_errors{0}, write_timeouts{0}, transitions{0}, clips{0}, playback_clips{0};
    std::atomic<unsigned> capture_stack{0}, playback_stack{0};
    std::atomic<float> rms_db{-120}, peak_db{-120}, bands[3]{{-120},{-120},{-120}};
    #if CONFIG_DG_FAST_MIC_DIAGNOSTICS
    using DiagnosticScalar=float;
#else
    using DiagnosticScalar=double;
#endif
    std::atomic<uint64_t> playback_total_us{0}, playback_frames{0};
#if CONFIG_DG_PLAYBACK_BREAKDOWN
    dg::PlaybackCost pull_cost,i2s_cost;
#endif
    std::atomic<unsigned> playback_max_us{0};
    std::atomic<unsigned> measure_total_us{0}, measure_max_us{0}, measure_frames{0};
    DiagnosticScalar q1[3]{}, q2[3]{}, sum=0; unsigned diagnostic_samples=0; int peak=0;
    void measure(const int16_t* pcm, size_t n) {
        constexpr DiagnosticScalar coefficients[3]={DiagnosticScalar(1.847759065),DiagnosticScalar(1.662939225),DiagnosticScalar(1.414213562)};
        for(size_t i=0;i<n;++i) {
            DiagnosticScalar x=pcm[i]/DiagnosticScalar(32768); sum+=x*x; peak=std::max(peak,std::abs(int(pcm[i])));
            for(unsigned k=0;k<3;++k) { DiagnosticScalar q=x+coefficients[k]*q1[k]-q2[k]; q2[k]=q1[k]; q1[k]=q; }
            if(++diagnostic_samples==16000) {
                rms_db=10*std::log10(sum/16000+1e-12); peak_db=20*std::log10(peak/32768.0+1e-6);
                for(unsigned k=0;k<3;++k) {
                    bands[k]=10*std::log10(std::max(DiagnosticScalar(0),DiagnosticScalar(2)*(q1[k]*q1[k]+q2[k]*q2[k]-coefficients[k]*q1[k]*q2[k]))/(16000.0*16000)+1e-12);
                    q1[k]=q2[k]=0;
                }
                sum=0; peak=0; diagnostic_samples=0;
            }
        }
    }
    std::thread capture, playback;
    Audio send;PlaybackRead read;
    std::function<bool()> media_ready;
    std::function<void(const int16_t*, size_t)> record;
    // Only playback_run owns this buffer. Keep it in the large pipeline heap
    // object (PSRAM under the board allocator policy), off the internal stack
    // that also carries the Opus decode call chain.
    int16_t playback_pcm[320]{};
    int16_t* window = nullptr;
    float* fft = nullptr;
    bool closed = false;
    Impl(Audio s, PlaybackRead r, std::function<void(const int16_t*, size_t)> rec, std::function<bool()> ready): send(s), read(r), media_ready(ready), record(rec) {
#if CONFIG_DG_ECHOEAR_AEC
        gate.set_aec_active(true);
        gate.guard_ms(dg_audio::aec_config::guard_ms);
        gate.rearm_ms(dg_audio::aec_config::rearm_ms);
        if(auto log=codec::hardware().log_aec_guard)log(gate.guard_ms(),gate.rearm_ms());
        speaker.onset_observer=[this](bool pending,int64_t first,int64_t end) {
            std::lock_guard<std::mutex> lock(gate_mutex);
            if(pending)gate.prepare_write(first);
            else {
                const auto before=gate.onsets();const auto previous_end=gate.last_end();gate.finish_write(first,end);
                if(gate.onsets()!=before)AUDIO_LOG("AEC_GUARD onset_upper_us=%lld source_deadline_us=%lld guard_ms=%u rearm_ms=%u previous_end_us=%lld",(long long)first,(long long)gate.guard_until(),gate.guard_ms(),gate.rearm_ms(),(long long)previous_end);
            }
        };
#endif
        output_volume_db=codec::hardware().default_output_gain_db;
        speaker.volume(unsigned(std::lround(32768*std::pow(10.,output_volume_db.load()/20.))));
        speaker.audible_threshold(playback_target?1:std::max(1u,32*playback_gain.load()/100));
        window = static_cast<int16_t*>(heap_caps_malloc(2048, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        fft = static_cast<float*>(heap_caps_malloc(10240, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        if (!window || !fft) { free(window); free(fft); throw std::bad_alloc(); }
        dg::PthreadConfig threads;auto old=threads.current();
        auto cfg = old; cfg.pin_to_core=1; cfg.stack_alloc_caps=MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
        try {
            cfg.prio=12; cfg.stack_size=16384; cfg.thread_name="audio-capture";
            threads.set(cfg); capture=std::thread([this]{ capture_run(); });
            cfg.prio=13; cfg.stack_size=CONFIG_DG_PLAYBACK_STACK_BYTES; cfg.thread_name="audio-playback";
            cfg.stack_alloc_caps=MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
            threads.set(cfg); playback=std::thread([this]{ playback_run(); });
        } catch (...) { stop=true; if(capture.joinable()) capture.join(); free(window); free(fft); throw; }
    }
    ~Impl() { stop=true; capture.join(); playback.join(); free(window); free(fft);
#if CONFIG_DG_SPEAKER_DIAGNOSTICS
        free(diagnostic_buffer);
#endif
    }
    void capture_run() {
        int16_t pcm[320]; size_t fill=0; unsigned frames=0, epoch=0, settling=0;
        while (!stop) {
            if(dg_heap_shadow_sample)dg_heap_shadow_sample();
            if (benchmark_paused) { capture_parked=true; delay(); continue; }
            capture_parked=false;
            const auto before_clips=mic.clips();
#if CONFIG_DG_SPEAKER_DIAGNOSTICS
            int16_t raw_pcm[320];
            const bool recording_raw=diagnostic_enabled.load();
            auto n=mic.read(pcm,320,100,recording_raw?raw_pcm:nullptr);
            if(recording_raw) {
                std::lock_guard<std::mutex> lock(diagnostic_mutex);
                if(diagnostic_enabled && diagnostic_count+n<=16000*30) {
                    std::memcpy(diagnostic_buffer+diagnostic_count,raw_pcm,n*2); diagnostic_count+=n;
                }
            }
#else
            auto n=mic.read(pcm,320,100);
#endif
            if(n!=320) ++read_errors;
#if CONFIG_DG_SPEAKER_DIAGNOSTICS
            if (fixture_enabled.load()) {
                std::lock_guard<std::mutex> lock(fixture_mutex);
                if (fixture_pcm) {
                    for (size_t i=0; i<n; ++i) {
                        pcm[i]=fixture_pcm[fixture_at++];
                        if (fixture_at==fixture_count) fixture_at=0;
                    }
                }
            }
#endif
            clips=mic.clips();
            // A new capture session discards 100 ms while the filter settles.
            // Send silence for those intervals to preserve transport cadence.
            const auto current_epoch=capture_epoch.load();
            if(current_epoch!=epoch) {epoch=current_epoch;settling=1600;}
            unsigned turn_clips=0;
            if(settling) {
                const auto discarded=std::min(size_t(settling),n);
                std::fill_n(pcm,discarded,0);settling-=discarded;
            } else if(capture_enabled) {
                turn_clips=unsigned(mic.clips()-before_clips);
                in_turn_mic_clips.fetch_add(turn_clips);
            }
#if CONFIG_DG_PHASE1_DIAGNOSTICS
            auto measure_begin=esp_timer_get_time();
#endif
            measure(pcm,n);
#if CONFIG_DG_PHASE1_DIAGNOSTICS
            unsigned measure_us=esp_timer_get_time()-measure_begin;
            measure_total_us+=measure_us; measure_max_us=std::max(measure_max_us.load(),measure_us); ++measure_frames;
#endif
            if (!silent_reports) {
                for(size_t i=0;i<n;++i) { window[fill++]=pcm[i]; if(fill==1024) { mic_report(window,fft,fft+1024,fft+2048); fill=0; } }
            }
            double pre=0;
#if CONFIG_DG_FAST_MIC_DIAGNOSTICS
            // PCM16 power fits in uint64_t; the former double sum was exact
            // too. Convert once for the existing dB calculation.
            uint64_t energy=0;
            for(size_t i=0;i<n;++i) energy+=int32_t(pcm[i])*pcm[i];
            pre=double(energy);
#else
            for(size_t i=0;i<n;++i) pre+=double(pcm[i])*pcm[i];
#endif
            bool muted; auto now=esp_timer_get_time();
            { std::lock_guard<std::mutex> lock(gate_mutex);
#if CONFIG_DG_ECHOEAR_AEC
                muted=gate.capture_closed(now,mic.capture_clock().lower_us(),mic.capture_clock().valid());
#else
                muted=gate.closed(now);
#endif
            }
            character_signals.capture(uint32_t(now/1000),uint32_t(pre/std::max(size_t(1),n)),capture_enabled && !muted);
            if(muted!=closed) { closed=muted; ++transitions; AUDIO_LOG("GATE %s t=%lld source_lower_us=%lld source_valid=%d",muted?"closed":"open",(long long)now,(long long)mic.capture_clock().lower_us(),mic.capture_clock().valid()); }
            // Classify by the same gate decision that mutes this PCM block.
            // Closed-gate saturation still fails the front-end clip gate.
            (muted ? gate_closed_mic_clips : gate_open_mic_clips).fetch_add(turn_clips);
            if(muted) std::memset(pcm,0,n*2);
            double post=0;
#if CONFIG_DG_FAST_MIC_DIAGNOSTICS
            // Gating only replaces the complete frame with zero.
            post=muted?0:pre;
#else
            for(size_t i=0;i<n;++i) post+=double(pcm[i])*pcm[i];
#endif
            if(capture_enabled && n && send(pcm,n)!=int(n)) ++drops;
            if(capture_enabled && ++frames%5==0) AUDIO_LOG("MIC_GATE t=%lld closed=%d pre=%.1f post=%.1f",(long long)now,muted,10*log10(pre/std::max(size_t(1),n)/1073741824.0+1e-12),10*log10(post/std::max(size_t(1),n)/1073741824.0+1e-12));
            capture_stack=uxTaskGetStackHighWaterMark(nullptr);
        }
    }
    int64_t last_write_us=0; // Guarded by speaker_mutex, including console tones.
    size_t write(const int16_t* pcm,size_t n,bool eligible=true,uint32_t timeout_ms=100) {
        // Close before enqueueing the first audible sample. Refresh after the
        // write with the configured DMA residence included in the tail.
        constexpr int64_t dma_residence_us=(int64_t(CONFIG_DG_SPEAKER_DMA_DESCRIPTORS)*CONFIG_DG_SPEAKER_DMA_FRAMES*1000000+15999)/16000;
        const int threshold=playback_target?1:std::max(1u,32*playback_gain.load()/100);
        bool active=false; for(size_t i=0;i<n;++i) active |= std::abs(int(pcm[i]))>threshold;
        if(active && eligible) { std::lock_guard<std::mutex> lock(gate_mutex); gate.audible(esp_timer_get_time()+dma_residence_us); }
        auto previous_errors = speaker.errors();
        auto previous_timeouts = speaker.timeouts();
#if CONFIG_DG_PLAYBACK_BREAKDOWN
        auto i2s_begin=esp_timer_get_time();
#endif
        const auto written=speaker.write(pcm,n,timeout_ms,eligible);
        if(speaker.errors()!=previous_errors) {++write_errors; ++hardware_write_errors;}
        if(speaker.timeouts()!=previous_timeouts) {++write_timeouts; ++hardware_write_timeouts;}
#if CONFIG_DG_PLAYBACK_BREAKDOWN
        i2s_cost.add(i2s_begin);
#endif
        last_write_us=esp_timer_get_time();
        if(eligible && speaker.audible()) { std::lock_guard<std::mutex> lock(gate_mutex); gate.audible(esp_timer_get_time()+dma_residence_us); }
        return written;
    }
    // A rendered interval owns its DMA space even if media is cancelled or
    // late. Retire the PCM, not the write credit; silence cannot rearm the gate.
    // Ignore tick validity until all zeros are written: the schedule already
    // consumed this interval, so expiry cannot revoke its DMA write credit.
    void retire_interval(int16_t* pcm,size_t remaining) {
        std::fill_n(pcm,remaining,0);
        write_interval(pcm,remaining,false,[]{return true;});
    }
    // Every retried write of a rendered interval goes through here, under
    // speaker_mutex. It writes while `owned()` says the interval may still put
    // these samples out, and returns how many the driver accepted.
    //
    // A full DMA ring frees a descriptor within one descriptor period, so no
    // accepted sample for write_stall_us means the driver is not consuming at
    // all. `owned()` cannot be relied on to end the loop then: a tick stays
    // valid for as long as the DMA reports no completion. So the loop gives up
    // the rest of the interval, reports the stall and returns. A dead driver
    // must not keep speaker_mutex, and with it every speaker control, for ever.
    // The interval is not replayed and no media is read again for it; the
    // playback task tries again at its next opportunity.
    template<class Owned> size_t write_interval(const int16_t* pcm,size_t n,bool eligible,Owned owned,bool* stalled=nullptr) {
        size_t offset=0;int64_t accepted_us=esp_timer_get_time();
        while(offset<n && !stop && owned()) {
            const unsigned timeouts=write_timeouts;
            const auto written=write(pcm+offset,n-offset,eligible,5);
            offset+=written;
            if(write_stalled(written,accepted_us,timeouts)) {note_stall();if(stalled)*stalled=true;break;}
        }
        return offset;
    }
    static constexpr int64_t write_stall_us=200000;
    std::atomic<unsigned> write_stalls{0};
    bool write_stalled(size_t written,int64_t& accepted_us,unsigned timeouts_before) {
        const auto now=esp_timer_get_time();
        if(written) {accepted_us=now;return false;}
        if(now-accepted_us>=write_stall_us)return true;
        // A full ring blocks for the write timeout. A write that failed
        // without waiting, as a driver error does, must not spin.
        if(write_timeouts==timeouts_before)vTaskDelay(1);
        return false;
    }
    void note_stall() {
        const unsigned count=++write_stalls;
        if((count&(count-1))==0)AUDIO_LOG("SPEAKER_STALL count=%u t=%lld",count,(long long)esp_timer_get_time());
    }
    unsigned idle_playback(int16_t* pcm,int64_t last_read,bool& history) {
        bool reset_logged=false;uint64_t reset_count=0;int64_t reset_idle_us=0,reset_at=0;
        unsigned wait_us=5000;
        {
        std::lock_guard<std::mutex> lock(speaker_mutex);
        if(!speaker.running())return wait_us;
        // Keep consuming the output clock even when Playout briefly reports
        // Idle after mute expiry. Parking an enabled TX lets IDF abandon the
        // partial writer descriptor and invalidates the sample ledger.
        if(!history) {
            speaker.begin_drain();
            if(speaker.drained()) {speaker.idle();return wait_us;}
        }
        auto opportunity=speaker.opportunity(esp_timer_get_time());
        if(opportunity.refill_samples)speaker.refill(opportunity.refill_samples);
        wait_us=opportunity.wait_us;
        if(opportunity.ready) {
            speaker.render(opportunity.tick);
            std::fill_n(pcm,320,0);
            size_t tail=0;
            if(history && esp_timer_get_time()-last_read>=100000) {
                tail=dsp.flush(pcm,320);
                dsp.reset();history=false;dsp_resets=dsp.resets();
                limiter_samples=dsp.engagements();active_samples=dsp.active_samples();peak_exceedances=dsp.peak_exceedances();
                reset_logged=true;reset_count=dsp.resets();reset_at=esp_timer_get_time();reset_idle_us=reset_at-last_read;
            }
            write_interval(pcm,320,tail!=0,[&]{return speaker.valid(opportunity.tick);});
            if(!history)speaker.begin_drain();
        }
        hardware_underflows=speaker.underflows();
        }
        if(reset_logged)AUDIO_LOG("DSP_RESET count=%llu idle_us=%lld t=%lld",(unsigned long long)reset_count,
            (long long)reset_idle_us,(long long)reset_at);
        return wait_us;
    }
    void playback_run() {
        auto* pcm=playback_pcm;
        int64_t last_read=0;bool history=false;uint64_t epoch=0;dg::MediaToken previous_token;
        {std::lock_guard<std::mutex> lock(playback_wake_mutex);playback_task=xTaskGetCurrentTaskHandle();}
        while(!stop) {
            if(dg_heap_shadow_sample)dg_heap_shadow_sample();
            // Release the retired source before any bench park or pause.
            if(previous_token.source && !previous_token.valid()) {
                std::lock_guard<std::mutex> lock(speaker_mutex);
                dsp.reset();history=false;previous_token={};
            }
#if CONFIG_DG_PLAYBACK_BENCH
            if(auto ms=pause_ms.exchange(0)) {
                AUDIO_LOG("PLAYBACK_PAUSE begin ms=%u",ms);
                vTaskDelay(pdMS_TO_TICKS(ms));
                AUDIO_LOG("PLAYBACK_PAUSE end ms=%u",ms);
                continue; // Recheck media validity before any subsequent park.
            }
#endif
            if (benchmark_paused) { playback_parked=true; delay(); continue; }
            playback_parked=false;
            if(media_ready && !media_ready()) {
                previous_token={};
                auto wait_us=idle_playback(pcm,last_read,history);
                ulTaskNotifyTake(pdTRUE,std::max(TickType_t(1),pdMS_TO_TICKS(wait_us/1000)));
                continue;
            }
            SpeakerClock::Opportunity opportunity;
            {
                std::lock_guard<std::mutex> lock(speaker_mutex);
                speaker.start();opportunity=speaker.opportunity(esp_timer_get_time());
                if(opportunity.refill_samples)speaker.refill(opportunity.refill_samples);
                if(opportunity.ready) {
                    speaker.render(opportunity.tick);
                    if(epoch!=opportunity.tick.device_epoch) {dsp.reset();history=false;epoch=opportunity.tick.device_epoch;}
                }
            }
            if(!opportunity.ready) {
                ulTaskNotifyTake(pdTRUE,std::max(TickType_t(1),pdMS_TO_TICKS(opportunity.wait_us/1000)));
                continue;
            }
            // Waiting precedes the media lease/read mutex. A unique scheduled
            // output interval remains owned across decoder and short writes.
            dg::render_tick=opportunity.tick;
#if CONFIG_DG_PLAYBACK_BREAKDOWN
            auto pull_begin=esp_timer_get_time();
#endif
            dg::MediaToken token;
            int n=read(pcm,320,token);
#if CONFIG_DG_PLAYBACK_BREAKDOWN
            pull_cost.add(pull_begin);
#endif
            if(n>0) {
                last_read=esp_timer_get_time();history=true;
                record(pcm,n);
                std::lock_guard<std::mutex> lock(speaker_mutex);
                if(!token.valid()) {dsp.reset();history=false;previous_token={};retire_interval(pcm,320);continue;}
                // An expired device tick retires PCM, but its media source and
                // DSP history remain valid. Only media invalidation resets DSP.
                if(!speaker.valid(opportunity.tick)) {retire_interval(pcm,320);continue;}
                if(!token.same(previous_token)) {dsp.reset();previous_token=token;}
                auto active_before=dsp.active_samples();auto process_begin=esp_timer_get_time();
                dsp.process(pcm,n);
                unsigned process_us=esp_timer_get_time()-process_begin;
                playback_total_us+=process_us;++playback_frames;
                playback_max_us=std::max(playback_max_us.load(),process_us);
                limiter_samples=dsp.engagements();active_samples=dsp.active_samples();peak_exceedances=dsp.peak_exceedances();
                // Retain only the unwritten suffix of this interval. A
                // timeout never authorizes a second transport decision.
                bool stalled=false;
                const auto offset=write_interval(pcm,size_t(n),!playback_target || dsp.active_samples()>active_before,
                    [&]{return speaker.valid(opportunity.tick) && token.valid();},&stalled);
                // A driver that takes no PCM takes no silence either: the suffix is dropped.
                if(!stalled && offset<size_t(n))retire_interval(pcm,size_t(n)-offset);
                if(!token.valid()) {dsp.reset();history=false;previous_token={};}
            } else {
                std::fill_n(pcm,320,0);
                std::lock_guard<std::mutex> lock(speaker_mutex);
                if(speaker.valid(opportunity.tick))write(pcm,320,false,5);
            }
            hardware_underflows=speaker.underflows();
            playback_stack=uxTaskGetStackHighWaterMark(nullptr);
        }
        {std::lock_guard<std::mutex> lock(playback_wake_mutex);playback_task=nullptr;}
    }

};
Pipeline::Pipeline(Audio s,PlaybackRead r,std::function<void(const int16_t*,size_t)> rec,std::function<bool()> ready):impl_(new Impl(s,r,rec,ready)) {}
Pipeline::~Pipeline()=default;
void Pipeline::set_gate_policy(GatePolicy p) { std::lock_guard<std::mutex> lock(impl_->gate_mutex); impl_->gate.set_policy(p); }
Capability Pipeline::capability() const {
#if CONFIG_DG_ECHOEAR_AEC
    const auto status=aec_status();
    std::lock_guard<std::mutex> lock(impl_->gate_mutex);
    if(status.enabled)return {true,status.reference_gain_db,impl_->gate.guard_ms()};
#endif
    return {};
}
void Pipeline::aec_guard_ms(unsigned ms) {
    if(ms>1000)throw std::invalid_argument("AEC guard must be 0..1000 ms");
    if(impl_->capture_enabled)throw std::runtime_error("Disable capture before changing AEC guard");
    std::lock_guard<std::mutex> lock(impl_->gate_mutex);impl_->gate.guard_ms(ms);
    if(auto log=codec::hardware().log_aec_guard)log(ms,impl_->gate.rearm_ms());
}
void Pipeline::aec_guard_rearm_ms(unsigned ms) {
    if(!valid_guard_rearm_ms(ms))throw std::invalid_argument("AEC re-arm must be 500..10000 ms");
    if(impl_->capture_enabled)throw std::runtime_error("Disable capture before changing AEC re-arm");
    std::lock_guard<std::mutex> lock(impl_->gate_mutex);impl_->gate.rearm_ms(ms);
    if(auto log=codec::hardware().log_aec_guard)log(impl_->gate.guard_ms(),ms);
}
void Pipeline::speaking(bool active) { std::lock_guard<std::mutex> lock(impl_->gate_mutex); impl_->gate.speaking(active,esp_timer_get_time()); }
void Pipeline::enable_capture(bool enabled) {
    if(enabled && !impl_->capture_enabled) {
        ++impl_->capture_epoch;
#if CONFIG_DG_ECHOEAR_AEC
        std::lock_guard<std::mutex> lock(impl_->gate_mutex);impl_->gate.reset();
#endif
    }
    impl_->capture_enabled=enabled;
}
void Pipeline::quiet(bool enabled) { impl_->silent_reports=enabled; }
bool Pipeline::quiet() const {return impl_->silent_reports.load();}
void Pipeline::benchmark_idle(bool enabled) {
    impl_->benchmark_paused=enabled;
    if (enabled) {
        auto deadline=esp_timer_get_time()+500000;
        while (!impl_->capture_parked || !impl_->playback_parked) {
            if (esp_timer_get_time()>deadline) { impl_->benchmark_paused=false; throw std::runtime_error("Media workers did not park"); }
            delay();
        }
    }
}
void Pipeline::pause_playback(unsigned ms) {
#if CONFIG_DG_PLAYBACK_BENCH
    if(ms<200 || ms>1000)throw std::invalid_argument("Playback pause must be 200..1000 ms");
    impl_->pause_ms=ms;
#else
    (void)ms;throw std::runtime_error("Playback pause requires bench build");
#endif
}
void Pipeline::stats() {
    microphone_gain_status();
    ESP_LOGI(TAG,"OUTPUT_PATH volume_relative_db=%d accepted_pcm_peak=%u",output_volume_db.load(),impl_->speaker.peak());
#if CONFIG_DG_PLAYBACK_BREAKDOWN
    ESP_LOGI(TAG,"PLAYBACK_STAGES pull_calls=%llu pull_wall_us=%llu i2s_calls=%llu i2s_wall_us=%llu i2s_max_us=%u",
        (unsigned long long)impl_->pull_cost.calls.load(),(unsigned long long)impl_->pull_cost.wall_us.load(),
        (unsigned long long)impl_->i2s_cost.calls.load(),(unsigned long long)impl_->i2s_cost.wall_us.load(),impl_->i2s_cost.max_us.load());
#endif
    ESP_LOGI(TAG,"PLAYBACK_COST frames=%llu mean_us=%llu max_us=%u total_us=%llu",
        (unsigned long long)impl_->playback_frames.load(),
        (unsigned long long)(impl_->playback_total_us.load()/std::max(uint64_t(1),impl_->playback_frames.load())),
        impl_->playback_max_us.load(),(unsigned long long)impl_->playback_total_us.load());
    ESP_LOGI(TAG,"MIC_COST measure_frames=%u mean_us=%u max_us=%u fast=%u",impl_->measure_frames.load(),
        impl_->measure_total_us.load()/std::max(1u,impl_->measure_frames.load()),impl_->measure_max_us.load(),
#if CONFIG_DG_FAST_MIC_DIAGNOSTICS
        1u
#else
        0u
#endif
    );
    ESP_LOGI(TAG,"MIC_STATS rms=%.1f peak=%.1f clips=%u goertzel_1000=%.1f goertzel_1500=%.1f goertzel_2000=%.1f window_ms=1000",
        double(impl_->rms_db.load()),double(impl_->peak_db.load()),impl_->clips.load(),double(impl_->bands[0].load()),double(impl_->bands[1].load()),double(impl_->bands[2].load()));
    ESP_LOGI(TAG,"PLAYBACK gain_percent=%s clips=%u ceiling_percent=%d highpass_hz=%d limiter_samples=%llu i2s_underflows=%u i2s_queue_overflows=%u stack_bytes=%d",
        (impl_->playback_target?std::string("n/a"):std::to_string(impl_->playback_gain.load())).c_str(),impl_->playback_clips.load(),impl_->playback_ceiling,
        CONFIG_DG_PLAYBACK_HIGHPASS_HZ,(unsigned long long)impl_->limiter_samples.load(),hardware_underflows.load(),
        impl_->speaker.queue_overflows(),CONFIG_DG_PLAYBACK_STACK_BYTES);
    ESP_LOGI(TAG,"LOUDNESS target_dbfs=%d active_samples=%llu limiter_active_samples=%llu peak_exceedances=%llu lookahead_ms=%d resets=%llu limiter_attack_ms=%d limiter_delay_ms=%d",
        impl_->playback_target,(unsigned long long)impl_->active_samples.load(),
        (unsigned long long)impl_->limiter_samples.load(),(unsigned long long)impl_->peak_exceedances.load(),
        impl_->playback_target?2*CONFIG_DG_PLAYBACK_LOOKAHEAD_MS:0,(unsigned long long)impl_->dsp_resets.load(),
        impl_->playback_target?CONFIG_DG_PLAYBACK_LOOKAHEAD_MS:0,
        impl_->playback_target?2*CONFIG_DG_PLAYBACK_LOOKAHEAD_MS:0);
    ESP_LOGI(TAG,"AUDIO drops=%u read_errors=%u write_errors=%u clips=%u transitions=%u capture_stack=%u playback_stack=%u write_timeouts=%u",impl_->drops.load(),impl_->read_errors.load(),impl_->write_errors.load(),impl_->clips.load(),impl_->transitions.load(),impl_->capture_stack.load(),impl_->playback_stack.load(),impl_->write_timeouts.load());
}
void Pipeline::aec_probe(std::function<void()> onset) {
#if CONFIG_DG_ECHOEAR_AEC
    std::lock_guard<std::mutex> lock(impl_->speaker_mutex);
    if(impl_->media_ready && impl_->media_ready())throw std::runtime_error("AEC probe requires inactive media");
    AecProbe generator;int16_t pcm[320];const unsigned before=impl_->write_errors;
    onset();
    for(unsigned at=0;at<AecProbe::samples;at+=320) {
        for(unsigned i=0;i<320;++i)pcm[i]=generator.next(at+i);
        if(impl_->write(pcm,320)!=320)throw std::runtime_error("Incomplete I2S AEC probe write");
    }
    if(impl_->write_errors!=before)throw std::runtime_error("I2S AEC probe write");
#else
    (void)onset;throw std::runtime_error("AEC probe requires experimental image");
#endif
}
void Pipeline::tone(unsigned hz,unsigned ms,unsigned amp,std::function<void()> onset) {
    if(hz<100 || hz>7800 || !ms || ms>10000 || !amp || amp>100) throw std::runtime_error("tone range");
    std::lock_guard<std::mutex> lock(impl_->speaker_mutex);
    // Direct tone writes bypass render ticks and would park a live consumer
    // for the tone duration, expiring its scheduled intervals.
    if(impl_->media_ready && impl_->media_ready())
        throw std::runtime_error("Direct tone requires inactive media");
    int16_t pcm[320], sine[320];
    for(unsigned i=0;i<320;++i) sine[i]=int16_t(32767.0*amp/100*std::sin(2*3.141592653589793*hz*i/16000));
    onset(); unsigned before=impl_->write_errors;
    for(unsigned at=0,total=ms*16;at<total;) {
        unsigned n=std::min(320u,total-at);
        for(unsigned i=0;i<n;++i) {
            // Integer-Hz frequencies need not divide a 20 ms frame.
            float env=std::min(1.0f,std::min(float(at+i)/160,float(total-at-i)/160));
            pcm[i]=int16_t(env*(hz%50==0?sine[i]:32767.0*amp/100*std::sin(2*3.141592653589793*hz*(at+i)/16000)));
        }
        impl_->write(pcm,n); at+=n;
    }
    if(impl_->write_errors!=before) throw std::runtime_error("I2S tone write");
}

}

namespace dg_audio {
void Pipeline::diagnostic_fixture(const int16_t* pcm, size_t samples) {
#if CONFIG_DG_SPEAKER_DIAGNOSTICS
    if (impl_->capture_enabled) throw std::runtime_error("Disable capture before setting fixture");
    if (pcm && !samples) throw std::invalid_argument("Empty microphone fixture");
    std::lock_guard<std::mutex> lock(impl_->fixture_mutex);
    impl_->fixture_pcm=pcm; impl_->fixture_count=samples; impl_->fixture_at=0;
    impl_->fixture_enabled=pcm!=nullptr;
#else
    (void)pcm; (void)samples; throw std::runtime_error("Speaker diagnostics disabled");
#endif
}
void Pipeline::diagnostic_capture(bool enabled) {
#if CONFIG_DG_SPEAKER_DIAGNOSTICS
    std::lock_guard<std::mutex> lock(impl_->diagnostic_mutex);
    if(enabled && !impl_->diagnostic_buffer) {
        impl_->diagnostic_buffer=static_cast<int16_t*>(heap_caps_malloc(16000*30*2,MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        if(!impl_->diagnostic_buffer) throw std::bad_alloc();
    }
    if(enabled) impl_->diagnostic_count=0;
    impl_->diagnostic_enabled=enabled;
#else
    (void)enabled; throw std::runtime_error("Speaker diagnostics disabled");
#endif
}
size_t Pipeline::diagnostic_pcm(const int16_t*& pcm) {
#if CONFIG_DG_SPEAKER_DIAGNOSTICS
    std::lock_guard<std::mutex> lock(impl_->diagnostic_mutex);
    if(impl_->diagnostic_enabled) throw std::runtime_error("Stop diagnostic capture before dump");
    pcm=impl_->diagnostic_buffer; return impl_->diagnostic_count;
#else
    (void)pcm; throw std::runtime_error("Speaker diagnostics disabled");
#endif
}
void Pipeline::reference_gain(unsigned db) {
#if CONFIG_DG_ECHOEAR_AEC
    if(impl_->capture_enabled)throw std::runtime_error("Disable uplink before changing reference gain");
    codec::reference_analog_gain(db);
#else
    (void)db;throw std::runtime_error(codec::hardware().reference_gain_error);
#endif
}
unsigned Pipeline::reference_gain_status() {
#if CONFIG_DG_ECHOEAR_AEC
    return codec::reference_gain_status();
#else
    throw std::runtime_error(codec::hardware().reference_gain_error);
#endif
}
void Pipeline::microphone_analog_gain(unsigned db) {
    if(impl_->capture_enabled) throw std::runtime_error("Disable capture before changing analog gain");
    if(!codec::hardware().analog_gain_control)throw std::runtime_error(codec::hardware().analog_gain_error);
    if(db!=24 && db!=30 && db!=33) throw std::runtime_error("Analog gain must be 24, 30 or 33 dB");
    codec::microphone_analog_gain(db);
}
void Pipeline::microphone_gain_status() {
    if(!codec::hardware().analog_gain_control)return;
    codec::microphone_gain_status(impl_->mic.gain_percent());
    ESP_LOGI(TAG,"MIC_PROTECTION schema=1 knee=%u ceiling=%u limited_samples=%llu adc_clips=%llu",
        unsigned(mic_knee),unsigned(mic_ceiling),(unsigned long long)impl_->mic.limited_samples(),
        (unsigned long long)impl_->mic.adc_clips());
}
void Pipeline::microphone_gain(unsigned percent) {
    if (impl_->capture_enabled) throw std::runtime_error("Disable capture before changing microphone gain");
    impl_->mic.gain_percent(percent);
    ESP_LOGI(TAG,"MIC_GAIN percent=%u",percent);
}
void Pipeline::playback_volume(int db) {
    if(db < -36 || db > 0) throw std::runtime_error("Playback volume must be -36..0 dB relative to qualified maximum");
    std::lock_guard<std::mutex> lock(impl_->speaker_mutex);
    impl_->speaker.volume(unsigned(std::lround(32768*std::pow(10.,db/20.))));
    output_volume_db=db;
    ESP_LOGI(TAG,"OUTPUT_VOLUME relative_db=%d max_relative_db=0 default_relative_db=-6",db);
}
void Pipeline::playback_settings(unsigned gain,unsigned ceiling) {
#if CONFIG_DG_SPEAKER_DIAGNOSTICS
    if(!gain || gain>100 || !ceiling || ceiling>100) throw std::runtime_error("DSP settings range");
    std::lock_guard<std::mutex> lock(impl_->speaker_mutex);
    impl_->dsp=PlaybackDsp(gain/100.0f,ceiling/100.0f,CONFIG_DG_PLAYBACK_HIGHPASS_HZ);
    impl_->playback_gain=gain; impl_->playback_ceiling=ceiling; impl_->playback_target=0;
    impl_->limiter_samples=0;impl_->active_samples=0;impl_->peak_exceedances=0;impl_->dsp_resets=0;
    impl_->speaker.audible_threshold(std::max(1,32*int(gain)/100));
#else
    (void)gain; (void)ceiling; throw std::runtime_error("Speaker diagnostics disabled");
#endif
}
void Pipeline::playback_loudness(int target,unsigned ceiling) {
#if CONFIG_DG_SPEAKER_DIAGNOSTICS
    if(target<-40 || target>-12 || !ceiling || ceiling>100) throw std::runtime_error("Loudness settings range");
    std::lock_guard<std::mutex> lock(impl_->speaker_mutex);
    impl_->dsp=PlaybackDsp(ceiling/100.0f,CONFIG_DG_PLAYBACK_HIGHPASS_HZ,speech_config(target));
    impl_->playback_target=target;impl_->playback_ceiling=ceiling;
    impl_->limiter_samples=0;impl_->active_samples=0;impl_->peak_exceedances=0;impl_->dsp_resets=0;
    impl_->speaker.audible_threshold(1);
#else
    (void)target;(void)ceiling;throw std::runtime_error("Speaker diagnostics disabled");
#endif
}
}
