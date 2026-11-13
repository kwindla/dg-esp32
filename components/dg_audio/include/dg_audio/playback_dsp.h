#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
namespace dg_audio {
struct SpeechLevelConfig {
    float target_dbfs=-22;
    float seed_dbfs=-28.9f; // Effective post-compression source RMS at startup.
    float max_gain_db=18;
    float activity_dbfs=-45;
    float level_ms=400;
    float attack_ms=100;   // Reduce normalizer gain.
    float release_ms=1000; // Increase normalizer gain.
    float lookahead_ms=2; // Attack ramp L; total delay is 2L (at most 5 ms).
    float limiter_release_ms=75;
    float compression_ratio=3;
    float compression_threshold_dbfs=-18;
};
// Pure streaming DSP, independent of packet boundaries. The legacy constructor
// retains the step-8 staging for comparison; the speech constructor uses a
// gated active-RMS estimator and delayed peak gain limiting. No allocation or
// transcendental work per sample. Gain holds through inactive windows.
class PlaybackDsp {
public:
    PlaybackDsp(float gain,float ceiling,float highpass_hz=0,float sample_rate=16000);
    PlaybackDsp(float ceiling,float highpass_hz,const SpeechLevelConfig&,float sample_rate=16000);
    float sample(float x);
    void process(int16_t* pcm,size_t count);
    void reset(); // Clear filter/delay history, hold speech gain across pauses.
    size_t flush(int16_t* pcm,size_t capacity); // Drain delayed audio before reset.
    uint64_t engagements() const { return engagements_; }
    uint64_t active_samples() const { return active_samples_; }
    uint64_t peak_exceedances() const { return peak_exceedances_; }
    uint64_t resets() const { return resets_; }
    float normalizer_gain() const { return agc_gain_; }
    size_t latency_samples() const { return delay_samples_; }
private:
    // Power-of-two storage avoids integer division in per-sample ring indexing.
    // A <=5 ms window at supported rates <=32 kHz needs at most 161 entries.
    static constexpr size_t capacity=256;
    float gain_, ceiling_, knee_, b0_=1,b1_=0,b2_=0,a1_=0,a2_=0,z1_=0,z2_=0;
    bool speech_=false, active_=false;
    float compression_threshold_=0,compression_slope_=1,compression_width_=0,compression_knee_reciprocal_=0;
    float target_=0,max_gain_=1,activity_power_=0,level_alpha_=0,
          attack_alpha_=0,release_alpha_=0,limiter_alpha_=0,
          level_power_=0,window_power_=0,delivered_power_=0,agc_gain_=1,desired_gain_=1,limit_gain_=1,
          ramp_sum_=0,ramp_reciprocal_=0;
    unsigned window_samples_=0,window_size_=320;
    size_t delay_samples_=0,delay_at_=0,peak_head_=0,peak_count_=0;
    size_t ramp_samples_=0,ramp_at_=0,pending_samples_=0,hold_remaining_=0;
    uint32_t sample_index_=0;
    std::array<float,capacity> delay_{};
    std::array<float,capacity> ramp_{};
    std::array<bool,capacity> activity_{};
    std::array<float,capacity> peaks_{};
    std::array<uint32_t,capacity> peak_indices_{};
    uint64_t engagements_=0, active_samples_=0,peak_exceedances_=0,resets_=0;
};
}
