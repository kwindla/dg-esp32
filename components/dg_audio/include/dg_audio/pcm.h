#pragma once
#include <cstdint>
namespace dg_audio {
constexpr int16_t signed16(uint32_t value) {
    const auto bits=value&0xffffu;
    return static_cast<int16_t>(bits<0x8000u?int32_t(bits):int32_t(bits)-65536);
}
}
