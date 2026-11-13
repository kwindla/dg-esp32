#pragma once
#include "driver/i2s_common.h"
#include "dg_board/audio_hw.h"
#include "dg_audio/aec_config.h"
namespace dg_audio::codec {
inline const dg_board::AudioHw& hardware() {return dg_board::audio_hw(aec_config::enabled);}
unsigned capture_completions();
// Boot-lifetime codec/control ownership; Mic and Speaker own streaming state.
void init();
void release_rx(i2s_chan_handle_t);
void release_tx(i2s_chan_handle_t);
unsigned capture_overflows();
// GPIO-only transitions. DAC was unmuted/settled at init while PA was off.
void playback_active(bool active);
void microphone_analog_gain(unsigned db);
unsigned reference_analog_gain(unsigned db);
unsigned reference_gain_status();
unsigned microphone_gain_status(unsigned digital_percent);
i2s_chan_handle_t rx();
i2s_chan_handle_t tx();
}
