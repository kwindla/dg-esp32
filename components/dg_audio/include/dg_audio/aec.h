#pragma once
#include <cstddef>
#include <cstdint>
#include <array>

namespace dg_audio {
struct AecStatus {
    bool enabled=false;
    unsigned chunk_samples=0, adapter_delay_samples=0;
    int internal_bytes=0, psram_bytes=0;
    uint64_t calls=0, wall_us=0, input_samples=0, output_samples=0;
    uint64_t errors=0, reference_clips=0, output_clips=0;
    unsigned max_us=0;
    // Inclusive upper bounds in microseconds; final bucket is unbounded.
    std::array<uint32_t,8> duration_buckets{};
    uint64_t timing_rows=0,timing_row_us=0;
    unsigned timing_row_max_us=0;
    uint64_t reference_low_samples=0,reference_low_windows=0;
    unsigned reference_low_longest_samples=0;
    // Diagnostic settings, not acceptance gates (freeze interpretation in step 9).
    unsigned reference_gain_db=0;
    static constexpr unsigned reference_ratio_ppm_at_0db=1;
    static constexpr unsigned playback_power_threshold=32*32;
    static constexpr unsigned reference_window_samples=1600;

};
// Snapshots never perform I2C, processing, allocation or console IO.
AecStatus aec_status();
// Publish a successfully read-back codec gain; no codec IO on capture task.
void aec_reference_gain(unsigned db);
// Capture-only timing witness. Bounds cover ALL samples in this read block,
// including DMA residence; gaps invalidate acoustic timing/continuity.
struct AecReadTiming {int64_t lower_us=0,upper_us=0;bool gap=true;};
struct AecCaptureTiming {size_t start=0,end=0;AecReadTiming time;};
// Legacy pointer name retained; the actual interleaved stride is channels.
struct AecCapture {const int16_t* triples=nullptr;size_t frames=0;bool truncated=false;unsigned channels=3;const AecCaptureTiming* timing=nullptr;size_t timing_count=0;};
// Optional all-slots capture is MIC1,MIC2,MIC3,MIC4,processed. Legacy triple
// remains microphone,reference,processed; neither format invents alignment.
void aec_record(bool enabled,bool all_slots=false);
AecCapture aec_capture();
class EchoCanceller {
public:
    EchoCanceller();~EchoCanceller();
    EchoCanceller(const EchoCanceller&)=delete;
    EchoCanceller& operator=(const EchoCanceller&)=delete;
    bool process(const int16_t* microphone,const int16_t* reference,size_t n,int16_t* output,
                 const uint32_t* stereo32=nullptr,AecReadTiming timing={});
private:
    friend AecStatus aec_status();
    friend void aec_record(bool enabled,bool all_slots);
    friend AecCapture aec_capture();
    struct Impl;Impl* impl_=nullptr;
};
}
