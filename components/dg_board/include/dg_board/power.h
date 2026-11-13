#pragma once
#include <cstdint>
#include "board.h"
namespace dg_board {
struct PowerSnapshot {
    bool valid=false;int error=0;uint32_t samples=0,errors=0;
    int64_t sampled_us=0;unsigned voltage_mv=0,soc_percent=0;
    int current_ma=0,temperature_centideg_c=0;
};
namespace power {
void init();
inline bool available(){return info().has_battery;}
PowerSnapshot read();
}
}
