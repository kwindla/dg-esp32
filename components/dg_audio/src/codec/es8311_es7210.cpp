#include "dg_audio/aec_config.h"
#include "sdkconfig.h"
#include "audio_codec.h"
#include "dg_audio/reference_gain.h"
#include "dg_audio/aec.h"
#include "i2s_duplex.h"
#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_timer.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev_defaults.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <mutex>
#include <new>

namespace dg_audio::codec {
namespace {
std::once_flag initialized;
i2s_chan_handle_t input, output;
const audio_codec_if_t* playback_codec;
const audio_codec_if_t* capture_codec;
unsigned analog_gain_db=hardware().registers.analog_gain_db;
#if CONFIG_DG_ECHOEAR_AEC
static_assert(valid_reference_gain(dg_audio::aec_config::ref_gain_db), "AEC reference gain must be 0..33 dB by 3");
unsigned reference_gain_db=dg_audio::aec_config::ref_gain_db;
#endif
// Board-lifetime ES8311 control. Unlike the generic control, failures include
// register and esp_err; the vendor open() cannot accidentally swallow them.
i2c_master_dev_handle_t dac_device;
bool first_reg44 = true, first_reg0d_read = true;
const auto& hw=hardware();
const auto& profile=hw.registers;
const bool dac_no_reference=profile.no_dac_reference;
const int expected_reg44=profile.reg44.value;
audio_codec_ctrl_if_t dac_control{};
int dac_read(const audio_codec_ctrl_if_t*, int reg, int reg_len, void* data, int len) {
    ESP_ERROR_CHECK(reg_len == 1 && len == 1 ? ESP_OK : ESP_ERR_INVALID_ARG);
    const uint8_t address = reg;
    auto err = i2c_master_transmit_receive(dac_device, &address, 1, static_cast<uint8_t*>(data), len, 20);
    if (err != ESP_OK) ESP_LOGE(hw.codec_log_tag, "ES8311 read reg=0x%02x: %s", reg, esp_err_to_name(err));
    ESP_ERROR_CHECK(err);
    if(reg==profile.reg0d.reg && first_reg0d_read) {
        first_reg0d_read=false;
        ESP_LOGI(hw.codec_log_tag,"ES8311 initial REG0D=0x%02x",*static_cast<uint8_t*>(data));
    }
    return ESP_CODEC_DEV_OK;
}
int dac_write(const audio_codec_ctrl_if_t* ctrl, int reg, int reg_len, void* data, int len) {
    ESP_ERROR_CHECK(reg_len == 1 && len == 1 ? ESP_OK : ESP_ERR_INVALID_ARG);
    const uint8_t bytes[] = {static_cast<uint8_t>(reg), *static_cast<uint8_t*>(data)};
    const bool startup_reg44 = first_reg44 && reg == 0x44 && bytes[1] == 0x08;
    if (reg == 0x44) first_reg44 = false;
    auto err = i2c_master_transmit(dac_device, bytes, sizeof(bytes), 20);
    // esp_codec_dev 1.6.2 documents this one startup transaction as needing
    // a second write. Only its NACK is recoverable, and only with readback.
    if (startup_reg44 && err == ESP_ERR_INVALID_RESPONSE) {
        ESP_LOGW(hw.codec_log_tag, "ES8311 startup reg=0x44 value=0x08 NACK (%s); retry once", esp_err_to_name(err));
        vTaskDelay(pdMS_TO_TICKS(2));
        err = i2c_master_transmit(dac_device, bytes, sizeof(bytes), 20);
        ESP_ERROR_CHECK(err);
        uint8_t value = 0;
        dac_read(ctrl, reg, 1, &value, 1);
        ESP_ERROR_CHECK(value == 0x08 ? ESP_OK : ESP_ERR_INVALID_RESPONSE);
        ESP_LOGI(hw.codec_log_tag, "ES8311 startup REG44 retry verified=0x%02x", value);
    }
    if (err != ESP_OK) ESP_LOGE(hw.codec_log_tag, "ES8311 write reg=0x%02x value=0x%02x: %s", reg, bytes[1], esp_err_to_name(err));
    ESP_ERROR_CHECK(err);
    return ESP_CODEC_DEV_OK;
}
void probe_codecs(int64_t deadline) {
    bool dac_ready = false, adc_ready = false;
    while (esp_timer_get_time() < deadline) {
        if (!dac_ready) dac_ready = i2c_master_probe(dg_board::i2c::bus(), hw.dac_i2c_address, 5) == ESP_OK;
        if (!adc_ready) adc_ready = i2c_master_probe(dg_board::i2c::bus(), hw.adc_i2c_address, 5) == ESP_OK;
        if (dac_ready && adc_ready) {
            hw.log_codec_probe(true,dac_ready,adc_ready);
            return;
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    hw.log_codec_probe(false,dac_ready,adc_ready);
    ESP_ERROR_CHECK(ESP_ERR_TIMEOUT);
}
void initialize() {
    ESP_ERROR_CHECK(hw.codec==dg_board::CodecKind::Es8311Es7210 ? ESP_OK : ESP_ERR_INVALID_STATE);
    ESP_ERROR_CHECK(dg_board::i2c::init());
    if(hw.codec_rail.gpio>=0) {
        ESP_ERROR_CHECK(gpio_set_level(gpio_num_t(hw.codec_rail.gpio),hw.codec_rail.active_high));
        ESP_ERROR_CHECK(gpio_set_direction(gpio_num_t(hw.codec_rail.gpio),GPIO_MODE_OUTPUT));
    }
    ESP_ERROR_CHECK(gpio_set_level(gpio_num_t(hw.pa.gpio), !hw.pa.active_high));
    ESP_ERROR_CHECK(gpio_set_direction(gpio_num_t(hw.pa.gpio), GPIO_MODE_OUTPUT));
    const int64_t rail_deadline = esp_timer_get_time() + 200000;
    vTaskDelay(pdMS_TO_TICKS(50));

    initialize_i2s(input,output,hw.aec_reference);
    probe_codecs(rail_deadline);
    i2c_device_config_t dac_i2c{};
    dac_i2c.dev_addr_length = I2C_ADDR_BIT_LEN_7;
    dac_i2c.device_address = hw.dac_i2c_address;
    dac_i2c.scl_speed_hz = 400000;
    ESP_ERROR_CHECK(i2c_master_bus_add_device(dg_board::i2c::bus(), &dac_i2c, &dac_device));
    dac_control.is_open = [](const audio_codec_ctrl_if_t*) { return true; };
    dac_control.get_info = [](const audio_codec_ctrl_if_t*, audio_codec_ctrl_info_t* info) {
        *info = {}; info->type = AUDIO_CODEC_CTRL_I2C;
        info->i2c.addr = hw.dac_i2c_address<<1; info->i2c.port = 0;
        return ESP_CODEC_DEV_OK;
    };
    dac_control.read_reg = dac_read;
    dac_control.write_reg = dac_write;

    // esp_codec_dev owns each codec's tested register initialization. Use its
    // codec/control interfaces directly: the pipeline retains bounded I2S IO
    // and DMA callbacks rather than allowing a second data driver to retime it.
    audio_codec_i2c_cfg_t i2c{};
    i2c.port = 0; i2c.bus_handle = dg_board::i2c::bus(); i2c.clock_speed_hz = 400000;
    auto* dac_ctrl = &dac_control;
    i2c.addr = hw.adc_i2c_address<<1;
    auto* adc_ctrl = audio_codec_new_i2c_ctrl(&i2c);
    auto* gpio = audio_codec_new_gpio();
    if (!dac_ctrl || !adc_ctrl || !gpio) throw std::bad_alloc();
    es8311_codec_cfg_t dac_cfg{};
    dac_cfg.ctrl_if = dac_ctrl; dac_cfg.gpio_if = gpio;
    dac_cfg.codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC;
    dac_cfg.no_dac_ref = dac_no_reference;
    dac_cfg.pa_pin = profile.codec_pa_gpio; dac_cfg.use_mclk = profile.use_mclk;
    dac_cfg.hw_gain.pa_gain = profile.hardware_pa_gain_db;
    auto* dac = es8311_codec_new(&dac_cfg);
    playback_codec = dac;
    es7210_codec_cfg_t adc_cfg{};
    adc_cfg.ctrl_if = adc_ctrl;
    adc_cfg.mic_selected = profile.mic_mask;
    auto* adc = es7210_codec_new(&adc_cfg);
    capture_codec=adc;
    if (!dac || !adc) throw std::bad_alloc();
    esp_codec_dev_sample_info_t format{};
    format.sample_rate = hw.tx.clock.sample_rate_hz; format.channel = 2; format.bits_per_sample = hw.tx.data_bits;
    format.mclk_multiple = profile.mclk_multiple;
    ESP_ERROR_CHECK(dac->set_fs(dac, &format));
    // esp_codec_dev 1.6.2 halves valid bits for TDM fetched as stereo32.
    format.bits_per_sample=profile.adc_format_bits;
    ESP_ERROR_CHECK(adc->set_fs(adc, &format));
    ESP_ERROR_CHECK(adc->enable(adc, true));
    ESP_ERROR_CHECK(dac->enable(dac, true));
    // enable() reapplies ES7210's default gain while selecting microphones.
    // Set the requested gain afterwards, as esp_codec_dev_open() does.
    ESP_ERROR_CHECK(adc->set_mic_channel_gain(adc, ES7210_SEL_MIC1, float(profile.analog_gain_db)));
#if CONFIG_DG_ECHOEAR_AEC
    ESP_ERROR_CHECK(adc->set_reg(adc,0x45,reference_gain_register(reference_gain_db)));
    int reference_gain=0;
    ESP_ERROR_CHECK(adc->get_reg(adc,0x45,&reference_gain));
    ESP_ERROR_CHECK(reference_gain==int(reference_gain_register(reference_gain_db)) ? ESP_OK : ESP_ERR_INVALID_RESPONSE);
    aec_reference_gain(reference_gain_db);
    ESP_LOGI(hw.codec_log_tag,"UNQUALIFIED TDM MIC3_GAIN=0x%02x analog_db=%u %s",reference_gain,reference_gain_db,hw.tdm_clock_description);
#endif
    ESP_ERROR_CHECK(dac->set_vol(dac, float(profile.dac_volume_db)));
    // All codec I2C completes before the audio workers exist. PA remains low.
    ESP_ERROR_CHECK(dac->mute(dac, false));
    vTaskDelay(pdMS_TO_TICKS(5)+1);
    int mic_gain=0, dac_volume=0, system=0, gpio44=0;
    ESP_ERROR_CHECK(adc->get_reg(adc, profile.mic1_gain.reg, &mic_gain));
    ESP_ERROR_CHECK(dac->get_reg(dac, 0x32, &dac_volume));
    ESP_ERROR_CHECK(dac->get_reg(dac, profile.reg0d.reg, &system));
    ESP_ERROR_CHECK(dac->get_reg(dac, profile.reg44.reg, &gpio44));
    ESP_LOGI(hw.codec_log_tag, "startup readback REG0D=0x%02x REG44=0x%02x MIC1_GAIN=0x%02x", system, gpio44, mic_gain);
    ESP_ERROR_CHECK(mic_gain == profile.mic1_gain.value && system == profile.reg0d.value && gpio44 == expected_reg44 ? ESP_OK : ESP_ERR_INVALID_RESPONSE);
    ESP_LOGI(hw.codec_log_tag, "startup verified REG0D=0x%02x REG44=0x%02x MIC1_GAIN=0x%02x EXPECTED_REG44=0x%02x no_dac_ref=%d", system, gpio44, mic_gain, expected_reg44, dac_no_reference);
    ESP_LOGI(hw.codec_log_tag, "register readback MIC1_GAIN=0x%02x DAC_VOLUME=0x%02x", mic_gain, dac_volume);
    hw.log_codec_summary();
}
}
void init() { std::call_once(initialized, initialize); }
void release_rx(i2s_chan_handle_t) {}
void release_tx(i2s_chan_handle_t) {}
void microphone_analog_gain(unsigned db) {
    init();
    ESP_ERROR_CHECK(db == 24 || db == 30 || db == 33 ? ESP_OK : ESP_ERR_INVALID_ARG);
    int value=0;
    ESP_ERROR_CHECK(capture_codec->get_reg(capture_codec,0x43,&value));
    // 1.6.2 get_db(33) mistakenly returns GAIN_30DB. Use the pinned enum's
    // 0..33 dB 3 dB ladder directly, preserving MIC1's enable bits. MIC2/3
    // remain untouched, in particular the unselected hardware reference.
    ESP_ERROR_CHECK(capture_codec->set_reg(capture_codec,0x43,(value&0xf0)|(db/3)));
    analog_gain_db=db;
    microphone_gain_status(0);
}
unsigned reference_analog_gain(unsigned db) {
#if CONFIG_DG_ECHOEAR_AEC
    if(!valid_reference_gain(db))throw std::invalid_argument("Reference gain: 0..33 dB by 3");
    init();
    // Console/control ownership serializes codec changes. Failed writes/readback
    // return ERR; never publish the new selected gain on failure.
    if(capture_codec->set_reg(capture_codec,0x45,reference_gain_register(db))!=ESP_CODEC_DEV_OK)
        throw std::runtime_error("Reference gain write failed");
    int value=0;
    if(capture_codec->get_reg(capture_codec,0x45,&value)!=ESP_CODEC_DEV_OK ||
       value!=int(reference_gain_register(db)))throw std::runtime_error("Reference gain readback failed");
    reference_gain_db=db;aec_reference_gain(db);
    ESP_LOGI(hw.codec_log_tag,"AEC_REF_GAIN db=%u reg45=0x%02x",db,value);
    return unsigned(value);
#else
    (void)db;throw std::runtime_error("Reference gain requires AEC");
#endif
}
unsigned reference_gain_status() {
#if CONFIG_DG_ECHOEAR_AEC
    init();int value=0;
    if(capture_codec->get_reg(capture_codec,0x45,&value)!=ESP_CODEC_DEV_OK ||
       value!=int(reference_gain_register(reference_gain_db)))throw std::runtime_error("Reference gain readback failed");
    ESP_LOGI(hw.codec_log_tag,"AEC_REF_GAIN db=%u reg45=0x%02x",reference_gain_db,value);
    return reference_gain_db;
#else
    throw std::runtime_error("Reference gain requires AEC");
#endif
}
unsigned microphone_gain_status(unsigned digital_percent) {
    init();int value=0;
    ESP_ERROR_CHECK(capture_codec->get_reg(capture_codec,0x43,&value));
    ESP_ERROR_CHECK(value == int(0x10|analog_gain_db/3) ? ESP_OK : ESP_ERR_INVALID_RESPONSE);
    ESP_LOGI(hw.codec_log_tag,"MIC_GAIN_PATH analog_db=%u reg=0x%02x digital_percent=%u",analog_gain_db,value,digital_percent);
    return analog_gain_db;
}
i2s_chan_handle_t rx() { init(); return input; }
i2s_chan_handle_t tx() { init(); return output; }
}
