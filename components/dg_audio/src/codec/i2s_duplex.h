#pragma once
#include "driver/i2s_std.h"
namespace dg_audio::codec {
unsigned capture_completions();
unsigned capture_overflows();
// RX is initialized first and owns external codec MCLK/BCLK/WS. The later
// TX direction is the internal duplex slave, with a separate sampler clock.
void initialize_i2s(i2s_chan_handle_t& input,i2s_chan_handle_t& output,bool aec);
}
