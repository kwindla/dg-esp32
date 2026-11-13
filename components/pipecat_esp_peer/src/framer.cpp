#include "framer.h"
#include <algorithm>
#include <stdexcept>

namespace pipecat::esp_peer {
int64_t pts_distance(uint32_t value, uint32_t reference) {
    uint32_t delta = value - reference;
    return delta <= INT32_MAX ? int64_t(delta) : int64_t(delta) - (int64_t(1) << 32);
}
size_t Framer::push(const int16_t* samples, size_t count) {
    if (!samples && count) throw std::invalid_argument("Null PCM input");
    size_t accepted = 0;
    while (count) {
        size_t n = std::min(count, pcm_frame_samples - partial_);
        std::copy_n(samples, n, pending_.begin() + partial_);
        // Incomplete frames remain accepted even if the complete-frame queue
        // is full: the consumer may make space before this frame is finished.
        accepted += n;
        samples += n;
        count -= n;
        partial_ += n;
        if (partial_ != pcm_frame_samples) continue;
        if (size_ < capacity) {
            auto& frame = storage_[(head_ + size_) % capacity];
            frame.samples = pending_;
            frame.pts_ms = next_pts_;
            ++size_;
        } else {
            ++dropped_;
            accepted -= n;
        }
        next_pts_ += frame_ms;
        partial_ = 0;
    }
    return accepted;
}
bool Framer::pop(PcmFrame& out) {
    if (!size_) return false;
    out = storage_[head_];
    head_ = (head_ + 1) % capacity;
    --size_;
    return true;
}
void Framer::reset(uint32_t first_pts_ms) {
    partial_ = head_ = size_ = 0;
    next_pts_ = first_pts_ms;
    dropped_ = 0;
}
}
