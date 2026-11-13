#pragma once
#include <array>
#include <cstddef>
#include <cstdint>

namespace pipecat::esp_peer {
constexpr size_t pcm_frame_samples = 320; // 16 kHz mono, 20 ms
constexpr uint32_t frame_ms = 20;
struct PcmFrame {
    std::array<int16_t, pcm_frame_samples> samples{};
    uint32_t pts_ms = 0;
};
// Caller supplies four frames (PSRAM on device), and serializes access.
// Full queues drop newest whole frames. Partial input is retained. PTS advances
// even for dropped frames so overload does not compress the media timeline.
class Framer {
public:
    static constexpr size_t capacity = 4;
    explicit Framer(std::array<PcmFrame, capacity>& storage) : storage_(storage) {}
    // Consumes all input. Returns this call's samples retained (including an
    // incomplete frame); callers must not retry dropped samples. A previously
    // accepted partial frame can still be dropped when completed under overload.
    size_t push(const int16_t* samples, size_t count);
    bool pop(PcmFrame& out);
    void reset(uint32_t first_pts_ms = 0);
    // Capture stop/start is a discontinuity even though no PCM was pushed.
    void discontinuity() {dropped_+=size_+(partial_?1:0);head_=size_=partial_=0;next_pts_+=frame_ms;}
    size_t depth() const { return size_; }
    size_t pending_samples() const { return partial_; }
    uint64_t dropped() const { return dropped_; }
private:
    std::array<PcmFrame, capacity>& storage_;
    std::array<int16_t, pcm_frame_samples> pending_{};
    size_t partial_ = 0, head_ = 0, size_ = 0;
    uint32_t next_pts_ = 0;
    uint64_t dropped_ = 0;
};
// Signed modular distance, without implementation-defined unsigned->signed casts.
// Valid for media timestamps less than half the uint32 range apart.
int64_t pts_distance(uint32_t value, uint32_t reference);
}
