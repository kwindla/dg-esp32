#include "console_backend.h"
#include "sdkconfig.h"
#include "driver/uart.h"
#include "driver/uart_vfs.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include <atomic>
namespace dg_console {
namespace {
#if CONFIG_ESP_CONSOLE_UART
constexpr auto port=static_cast<uart_port_t>(CONFIG_ESP_CONSOLE_UART_NUM);
esp_timer_handle_t retry_timer;
std::atomic<TaskHandle_t> drainer{nullptr};
QueueHandle_t events;
std::atomic<unsigned> fifo_overflows{0},buffer_full{0},frame_errors{0},parity_errors{0};
void rx_events(void*) {
    uart_event_t event;
    for(;;)if(xQueueReceive(events,&event,portMAX_DELAY)==pdTRUE) {
        switch(event.type) {
        case UART_FIFO_OVF:++fifo_overflows;break;
        case UART_BUFFER_FULL:++buffer_full;break;
        case UART_FRAME_ERR:++frame_errors;break;
        case UART_PARITY_ERR:++parity_errors;break;
        default:break;
        }
    }
}
RxStats rx_stats() {return {fifo_overflows.load(),buffer_full.load(),frame_errors.load(),parity_errors.load()};}
void resume(void*) {xTaskNotifyGive(drainer.load());}
void init_driver() {
    uart_config_t config{};
    config.baud_rate=CONFIG_ESP_CONSOLE_UART_BAUDRATE;config.data_bits=UART_DATA_8_BITS;
    config.parity=UART_PARITY_DISABLE;config.stop_bits=UART_STOP_BITS_1;
    config.flow_ctrl=UART_HW_FLOWCTRL_DISABLE;config.source_clk=UART_SCLK_DEFAULT;
    ESP_ERROR_CHECK(uart_param_config(port,&config));
#if CONFIG_ESP_CONSOLE_UART_CUSTOM
    ESP_ERROR_CHECK(uart_set_pin(port,CONFIG_ESP_CONSOLE_UART_TX_GPIO,CONFIG_ESP_CONSOLE_UART_RX_GPIO,
        UART_PIN_NO_CHANGE,UART_PIN_NO_CHANGE));
#endif
    // Driver-backed RX is installed before READY, as in the probe: commands
    // received during startup stay buffered. No TX ring: drain attempts must
    // never wait for space (uart_write_bytes would wait even with a TX ring).
    ESP_ERROR_CHECK(uart_driver_install(port,8192,0,32,&events,0));
    uart_vfs_dev_use_driver(port);
    ESP_ERROR_CHECK(xTaskCreate(rx_events,"console-uart-rx",3072,nullptr,5,nullptr)==pdPASS?ESP_OK:ESP_ERR_NO_MEM);
    esp_timer_create_args_t timer{};timer.callback=resume;timer.name="console-retry";
    ESP_ERROR_CHECK(esp_timer_create(&timer,&retry_timer));
}
int send(const char* text,size_t size) {return uart_tx_chars(port,text,size);}
void wait() {
    // A 128-byte FIFO empties in 640 us at 2Mbaud. A full 1ms tick
    // between attempts would cap throughput below the measured 0.8*R budget.
    // Sleep the drain task on a timer notification rather than busy-spinning.
    drainer=xTaskGetCurrentTaskHandle();
    ulTaskNotifyTake(pdTRUE,0); // Discard an early/stale wake before arming.
    const auto error=esp_timer_start_once(retry_timer,250);
    if(error!=ESP_OK && error!=ESP_ERR_INVALID_STATE){vTaskDelay(1);return;}
    // INVALID_STATE means a prior one-shot is still armed. Timer failures or
    // unrelated notifications cannot abort the console or leave it stuck here.
    ulTaskNotifyTake(pdTRUE,pdMS_TO_TICKS(2)+1);
}
#else
void init_driver() {ESP_ERROR_CHECK(ESP_ERR_NOT_SUPPORTED);}
int send(const char*,size_t) {return -1;}
void wait() {vTaskDelay(1);}
RxStats rx_stats() {return {};}
#endif
bool is_connected() {return true;} // UART has no USB SOF/reader-presence signal.
}
const Backend uart_backend{init_driver,is_connected,send,wait,rx_stats};
}
