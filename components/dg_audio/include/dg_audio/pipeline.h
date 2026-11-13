#pragma once
#include "gate.h"
#include "capability.h"
#include "dg_media_token.h"
#include <atomic>
#include <functional>
#include <memory>
#include <cstdint>
#include <cstddef>
namespace dg_audio {
class Pipeline {
public:
    using Audio = std::function<int(int16_t*, size_t)>;
    using PlaybackRead = std::function<int(int16_t*, size_t, dg::MediaToken&)>;
    Pipeline(Audio send, PlaybackRead read, std::function<void(const int16_t*, size_t)> record, std::function<bool()> media_ready = {});
    ~Pipeline();
    void set_gate_policy(GatePolicy policy);
    void speaking(bool active);
    Capability capability() const;
    void aec_guard_ms(unsigned ms);
    void aec_guard_rearm_ms(unsigned ms);
    void enable_capture(bool enabled);
    void quiet(bool enabled);
    bool quiet() const;
    void benchmark_idle(bool enabled);
    void stats();
    void tone(unsigned hz, unsigned ms, unsigned amp, std::function<void()> onset);
    // Experimental AEC diagnostic only; direct output requires inactive media.
    void aec_probe(std::function<void()> onset);
    void diagnostic_capture(bool enabled);
    void pause_playback(unsigned ms);
    // Bench-only PCM substitution; storage must outlive the pipeline.
    // Configure only with capture disabled. Null disables substitution.
    void diagnostic_fixture(const int16_t* pcm, size_t samples);
    size_t diagnostic_pcm(const int16_t*& pcm);
    void playback_volume(int relative_db);
    void playback_settings(unsigned gain, unsigned ceiling);
    void playback_loudness(int target, unsigned ceiling);
    void microphone_gain(unsigned percent);
    void microphone_analog_gain(unsigned db);
    void reference_gain(unsigned db);
    unsigned reference_gain_status();
    void microphone_gain_status();
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
}
