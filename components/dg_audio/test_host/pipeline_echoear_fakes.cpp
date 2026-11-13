#include "codec/audio_codec.h"
#include "driver/i2s_std.h"
namespace dg_audio::codec {
// Use the real Speaker and pipeline, with only the codec/channel factory
// boundary faked. The same I2S fake records accepted post-volume PCM.
i2s_chan_handle_t tx() {
    i2s_chan_config_t cfg{};i2s_chan_handle_t channel=nullptr;
    ESP_ERROR_CHECK(i2s_new_channel(&cfg,&channel,nullptr));return channel;
}
void release_tx(i2s_chan_handle_t channel) {i2s_del_channel(channel);}
void playback_active(bool) {}
void microphone_analog_gain(unsigned) {}
unsigned microphone_gain_status(unsigned) {return 30;}
}
