#include "sdkconfig.h"
#include "dg_wifi_observe.h"
#include "wifi_observe_internal.h"
#if CONFIG_DG_WIFI_OBSERVE
#include "esp_idf_version.h"
#include "dg_board/cache.h"
static_assert(dg_board::native_wifi_observer,"WiFi completion observer requires a native audited driver");
#if ESP_IDF_VERSION_MAJOR!=6 || ESP_IDF_VERSION_MINOR!=1
#error "WiFi completion observer audited only for IDF 6.1; board capability required"
#endif
#include "esp_private/wifi.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_memory_utils.h"
#include "esp_attr.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <atomic>

namespace {
// The native callback is shared driver state: this opt-in component is its
// sole application owner. It never inspects/mutates buffers or calls the
// driver, logger, allocator, clock or any blocking primitive from the callback.
// IDF's PSRAM atomic workaround makes is_always_lock_free false globally,
// dispatching internal 32-bit storage to S32C1I and external storage to a lock.
// Pin these counters to DRAM and validate before registration. Never put them
// in the PSRAM history or assume the host's atomic traits describe this port.
alignas(4) DRAM_ATTR std::atomic<uint32_t> completed_ok{0},completed_fail{0};
static_assert(sizeof(std::atomic<uint32_t>)==4 && alignof(std::atomic<uint32_t>)>=4,
              "WiFi callback counters require aligned 32-bit storage");
dg_wifi_observation_t* history=nullptr;
dg_wifi_observation_status_t status{};
portMUX_TYPE history_guard=portMUX_INITIALIZER_UNLOCKED;
uint32_t previous_ok=0,previous_fail=0;
int64_t previous_end_us=0;
TaskHandle_t sampler=nullptr;
void tx_done(uint8_t interface,uint8_t*,uint16_t*,bool success) {
    if(interface!=WIFI_IF_STA)return;
    (success?completed_ok:completed_fail).fetch_add(1,std::memory_order_relaxed);
}
void sample_once() {
    dg_wifi_observation_t row{};
    row.counter_begin_us=previous_end_us;
    row.begin_us=esp_timer_get_time();
    wifi_ps_type_t ps=WIFI_PS_NONE;
    row.ps_error=esp_wifi_get_ps(&ps);
    if(row.ps_error==ESP_OK)row.ps_mode=ps;
    wifi_ap_record_t ap{};
    row.ap_error=esp_wifi_sta_get_ap_info(&ap);
    if(row.ap_error==ESP_OK) {
        row.rssi_dbm=ap.rssi;row.primary_channel=ap.primary;row.secondary_channel=ap.second;
        for(unsigned i=0;i<6;++i)row.bssid[i]=ap.bssid[i];
    }
    // Counters describe native STA completion callbacks since the previous
    // sample. Snapshot skew is bounded by this sampling operation, not by a
    // claim that these callbacks are exact air/ACK timestamps or per-RTP IDs.
    const uint32_t ok=completed_ok.load(std::memory_order_relaxed);
    const uint32_t failed=completed_fail.load(std::memory_order_relaxed);
    row.tx_done_success=ok-previous_ok;row.tx_done_failed=failed-previous_fail;
    previous_ok=ok;previous_fail=failed;
    row.end_us=esp_timer_get_time();
    portENTER_CRITICAL(&history_guard);
    row.sequence=++status.samples;
    history[(row.sequence-1)%DG_WIFI_OBSERVE_CAPACITY]=row;
    status.retained=status.samples<DG_WIFI_OBSERVE_CAPACITY?unsigned(status.samples):unsigned(DG_WIFI_OBSERVE_CAPACITY);
    status.overwritten=status.samples-status.retained;
    status.first_sequence=status.samples-status.retained+1;status.last_sequence=status.samples;
    portEXIT_CRITICAL(&history_guard);
    previous_end_us=row.end_us;
}
void run(void*) {
    // No catch-up burst after a slow driver read: timestamps expose late bins.
    for(;;) {sample_once();vTaskDelay(pdMS_TO_TICKS(1000));}
}
}
extern "C" esp_err_t dg_wifi_observe_start(const wifi_init_config_t* init) {
    if(status.enabled)return ESP_ERR_INVALID_STATE;
    status.enabled=true;status.capacity=DG_WIFI_OBSERVE_CAPACITY;
    status.record_bytes=sizeof(dg_wifi_observation_t);
    if(!init){status.startup_error=ESP_ERR_INVALID_ARG;return status.startup_error;}
    if(!esp_ptr_internal(&completed_ok) || !esp_ptr_internal(&completed_fail)) {
        status.startup_error=ESP_ERR_INVALID_STATE;return status.startup_error;
    }
    status.static_tx_buffers=init->static_tx_buf_num;status.cache_tx_buffers=init->cache_tx_buf_num;
    status.static_rx_buffers=init->static_rx_buf_num;status.dynamic_rx_buffers=init->dynamic_rx_buf_num;
    status.tx_buffer_type=init->tx_buf_type;status.ampdu_tx=init->ampdu_tx_enable;
    status.ampdu_rx=init->ampdu_rx_enable;status.rx_ba_window=init->rx_ba_win;
#ifdef CONFIG_ESP_WIFI_TX_BA_WIN
    status.tx_ba_window=CONFIG_ESP_WIFI_TX_BA_WIN;
#endif
    status.wifi_core=init->wifi_task_core_id;
    history=static_cast<dg_wifi_observation_t*>(heap_caps_calloc(DG_WIFI_OBSERVE_CAPACITY,
        sizeof(dg_wifi_observation_t),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    if(!history){status.startup_error=ESP_ERR_NO_MEM;return status.startup_error;}
    previous_end_us=esp_timer_get_time();
    status.startup_error=esp_wifi_set_tx_done_cb(tx_done);
    if(status.startup_error!=ESP_OK){heap_caps_free(history);history=nullptr;return status.startup_error;}
    status.tx_done_registered=true;
    if(xTaskCreatePinnedToCoreWithCaps(run,"wifi-observe",DG_WIFI_OBSERVE_STACK_BYTES,nullptr,1,
            &sampler,0,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT)!=pdPASS) {
        const auto error=esp_wifi_set_tx_done_cb(nullptr);
        // A failed unregistration is an explicitly reported startup failure;
        // callback state is static and still safe, even with no sampling task.
        status.tx_done_registered=error!=ESP_OK;
        heap_caps_free(history);history=nullptr;status.startup_error=ESP_ERR_NO_MEM;
        return status.startup_error;
    }
    return ESP_OK;
}
extern "C" dg_wifi_observation_status_t dg_wifi_observe_status() {
    portENTER_CRITICAL(&history_guard);auto copy=status;portEXIT_CRITICAL(&history_guard);return copy;
}
extern "C" bool dg_wifi_observe_read(uint64_t sequence,dg_wifi_observation_t* output) {
    if(!output || !sequence)return false;
    portENTER_CRITICAL(&history_guard);
    const bool valid=history && sequence>=status.first_sequence && sequence<=status.last_sequence &&
        history[(sequence-1)%DG_WIFI_OBSERVE_CAPACITY].sequence==sequence;
    if(valid)*output=history[(sequence-1)%DG_WIFI_OBSERVE_CAPACITY];
    portEXIT_CRITICAL(&history_guard);return valid;
}
#else
extern "C" esp_err_t dg_wifi_observe_start(const wifi_init_config_t*) {return ESP_ERR_NOT_SUPPORTED;}
extern "C" dg_wifi_observation_status_t dg_wifi_observe_status() {return {};}
extern "C" bool dg_wifi_observe_read(uint64_t,dg_wifi_observation_t*) {return false;}
#endif
