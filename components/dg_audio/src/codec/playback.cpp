#include "audio_codec.h"
#include "sdkconfig.h"
#include "driver/gpio.h"
#include "esp_log.h"
namespace dg_audio::codec {
void playback_active(bool active) {
    // No I2C, mutex, or delay on the playback task. The DAC stays unmuted
    // after initialization; PA-off plus TX silence defines intentional idle.
    ESP_ERROR_CHECK(gpio_set_level(gpio_num_t(hardware().pa.gpio),active==hardware().pa.active_high));
    ESP_LOGI(hardware().codec_log_tag,"PA active=%d DAC muted=0",active);
}
}
