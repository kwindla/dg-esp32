#pragma once
#include "framer.h"
#include <algorithm>
#include <array>

namespace pipecat::esp_peer {
// Optional 40 ms uplink batching keeps the capture/framer's 20 ms clock.
// A dropped predecessor never joins unrelated PCM into a compressed timeline.
class UplinkFrame {
public:
    bool push(const PcmFrame& frame, unsigned duration_ms) {
        if (duration_ms == 20) {
            std::copy(frame.samples.begin(), frame.samples.end(), samples.begin());
            pts_ms = frame.pts_ms;
            pending_ = false;
            return true;
        }
        if (pending_ && pts_distance(frame.pts_ms, pts_ms) == 20) {
            std::copy(frame.samples.begin(), frame.samples.end(), samples.begin() + pcm_frame_samples);
            pending_ = false;
            return true;
        }
        if (pending_) ++discarded;
        std::copy(frame.samples.begin(), frame.samples.end(), samples.begin());
        pts_ms = frame.pts_ms;
        pending_ = true;
        return false;
    }
    std::array<int16_t, pcm_frame_samples * 2> samples{};
    uint32_t pts_ms = 0;
    unsigned discarded = 0;
private:
    bool pending_ = false;
};
}
