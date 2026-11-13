#include "sdkconfig.h"
#include "i2s_duplex.h"
#include "dg_board/audio_hw.h"
#include "i2s_config.h"
#include "esp_log.h"
#include "esp_attr.h"
#include <atomic>
namespace dg_audio::codec {
namespace {
std::atomic<unsigned> rx_overflows{0},rx_completions{0};
// One RX ISR writer per counter: aligned loads/stores avoid an out-of-line RMW.
static_assert(sizeof(std::atomic<unsigned>)==4 && alignof(std::atomic<unsigned>)>=4);
bool IRAM_ATTR receive_overflow(i2s_chan_handle_t,i2s_event_data_t*,void*) {
    rx_overflows.store(rx_overflows.load(std::memory_order_relaxed)+1,std::memory_order_relaxed);return false;
}
bool IRAM_ATTR receive_complete(i2s_chan_handle_t,i2s_event_data_t*,void*) {
    rx_completions.store(rx_completions.load(std::memory_order_relaxed)+1,std::memory_order_relaxed);return false;
}
}
unsigned capture_completions() {return rx_completions.load(std::memory_order_relaxed);}
unsigned capture_overflows() {return rx_overflows.load(std::memory_order_relaxed);}
void initialize_i2s(i2s_chan_handle_t& input,i2s_chan_handle_t& output,bool aec) {
    const auto& hw=dg_board::audio_hw(aec);
    auto cc=channel_config(hw.rx);
    ESP_ERROR_CHECK(i2s_new_channel(&cc,&output,&input));
    auto sc=stream_config(hw.rx,hw.tx.data_gpio,hw.rx.data_gpio,hw.aec_reference);
    // RX is first: it owns external MCLK/BCLK/WS. IDF makes paired TX
    // the slave even though the allocation requested a master pair.
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(input,&sc));
    sc=stream_config(hw.tx,hw.tx.data_gpio,hw.rx.data_gpio,hw.aec_reference);
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(output,&sc));
    if(aec) {
        i2s_chan_info_t rx{},tx{};
        ESP_ERROR_CHECK(i2s_channel_get_info(input,&rx));
        ESP_ERROR_CHECK(i2s_channel_get_info(output,&tx));
        const auto& limits=hw.paired_clock_limits;
        ESP_ERROR_CHECK(rx.role==I2S_ROLE_MASTER && tx.role==I2S_ROLE_SLAVE &&
            rx.pair_chan==output && tx.pair_chan==input &&
            rx.mclk_hz>=limits.rx_mclk_min && rx.mclk_hz<=limits.rx_mclk_max &&
            tx.mclk_hz>=limits.tx_mclk_min && tx.mclk_hz<=limits.tx_mclk_max &&
            rx.bclk_hz>=limits.bclk_min && rx.bclk_hz<=limits.bclk_max &&
            tx.bclk_hz>=limits.bclk_min && tx.bclk_hz<=limits.bclk_max &&
            tx.mclk_hz>=limits.tx_mclk_bclk_min_ratio*tx.bclk_hz ? ESP_OK:ESP_ERR_INVALID_STATE);
        hw.log_paired_clocks(
            static_cast<unsigned long>(rx.mclk_hz),static_cast<unsigned long>(rx.bclk_hz),
            static_cast<unsigned long>(tx.mclk_hz),static_cast<unsigned long>(tx.bclk_hz));
    }
    if(aec) {
        i2s_event_callbacks_t callbacks{};callbacks.on_recv_q_ovf=receive_overflow;
        callbacks.on_recv=receive_complete;
        ESP_ERROR_CHECK(i2s_channel_register_event_callback(input,&callbacks,nullptr));
    }
    ESP_ERROR_CHECK(i2s_channel_enable(input));
}
}
