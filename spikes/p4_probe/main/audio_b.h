#pragma once
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev_defaults.h"
namespace probe_b {
esp_err_t register_rx(i2s_chan_handle_t rx);
// Recreate the duplex channels together; retain the step-1a DMA geometry outside AEC.
bool configure_dma(unsigned descriptors, unsigned frames, i2s_chan_handle_t& rx, i2s_chan_handle_t& tx);
void init(i2c_master_bus_handle_t bus, i2s_chan_handle_t rx, i2s_chan_handle_t tx,
          const audio_codec_if_t* adc, const audio_codec_if_t* dac, bool ready);
bool command(const char* line, bool loop_busy);
}
