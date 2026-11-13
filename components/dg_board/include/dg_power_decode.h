#pragma once
#include <cstdint>
namespace dg_board {
constexpr uint16_t gauge_word(uint8_t low,uint8_t high) {return uint16_t(low)|(uint16_t(high)<<8);}
constexpr int gauge_current(uint16_t value) {return value<32768 ? int(value) : int(value)-65536;}
constexpr int gauge_temperature_centideg(uint16_t decikelvin) {return int(decikelvin)*10-27315;}
}
