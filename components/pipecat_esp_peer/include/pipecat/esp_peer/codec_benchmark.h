#pragma once
#include <cstddef>
#include <cstdint>
#include <nlohmann/json.hpp>
namespace pipecat::esp_peer {
// Called only while disconnected, with media workers parked. Owns a temporary
// internal-stack worker; it never changes transport codec configuration.
nlohmann::json playback_selftest();
nlohmann::json codec_benchmark(const int16_t* speech, size_t samples);
nlohmann::json silence_resume_benchmark(const uint8_t* stream,size_t bytes);
}
