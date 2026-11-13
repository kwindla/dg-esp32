#include "sdkconfig.h"
#include "audio_codec.h"
#include "i2s_config.h"
#include <stdexcept>
namespace dg_audio::codec {
void init() {
    ESP_ERROR_CHECK(hardware().codec==dg_board::CodecKind::DirectI2s ? ESP_OK : ESP_ERR_INVALID_STATE);
}
i2s_chan_handle_t rx() {
    init();const auto& stream=hardware().rx;
    auto cc=channel_config(stream);i2s_chan_handle_t channel=nullptr;
    ESP_ERROR_CHECK(i2s_new_channel(&cc,nullptr,&channel));
    auto sc=stream_config(stream,I2S_GPIO_UNUSED,stream.data_gpio);
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(channel,&sc));
    ESP_ERROR_CHECK(i2s_channel_enable(channel));
    return channel;
}
i2s_chan_handle_t tx() {
    init();const auto& stream=hardware().tx;
    auto cc=channel_config(stream);i2s_chan_handle_t channel=nullptr;
    ESP_ERROR_CHECK(i2s_new_channel(&cc,&channel,nullptr));
    auto sc=stream_config(stream,stream.data_gpio,I2S_GPIO_UNUSED);
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(channel,&sc));
    return channel;
}
void release_rx(i2s_chan_handle_t channel) {i2s_channel_disable(channel);i2s_del_channel(channel);}
void release_tx(i2s_chan_handle_t channel) {i2s_del_channel(channel);}
void playback_active(bool) {}
unsigned capture_completions() {return 0;}
unsigned capture_overflows() {return 0;}
void microphone_analog_gain(unsigned) {throw std::runtime_error(hardware().analog_gain_error);}
unsigned microphone_gain_status(unsigned) {return 0;}
unsigned reference_analog_gain(unsigned) {throw std::runtime_error(hardware().reference_gain_error);}
unsigned reference_gain_status() {throw std::runtime_error(hardware().reference_gain_error);}
}
