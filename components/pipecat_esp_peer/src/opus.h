#pragma once
#include <cstddef>
#include <cstdint>
#include "encoder_settings.h"
namespace pipecat::esp_peer {
// Each codec is processed by one worker. Initialization may precede session
// publication; destruction follows worker join.
class OpusEncoder {
public:
    OpusEncoder(unsigned bitrate, unsigned complexity, bool fec, bool dtx, unsigned frame_ms = 20);
    ~OpusEncoder();
    size_t encode(const int16_t* pcm, uint8_t* output, size_t capacity);
    const EncoderSettings& settings() const {return settings_;}
private:
    void* handle_ = nullptr;
    unsigned pcm_bytes_ = 640;
    EncoderSettings settings_;
};
class OpusDecoder {
public:
    OpusDecoder();
    ~OpusDecoder();
    void reset();
    size_t decode(uint8_t* packet, size_t bytes, int16_t* output, size_t capacity_samples, bool plc);
private:
    void* handle_ = nullptr;
};
}
