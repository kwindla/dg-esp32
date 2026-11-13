#include "console.h"
#include "driver/uart.h"
#include "driver/uart_vfs.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/ringbuf.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <vector>

namespace probe {
namespace {
constexpr auto port = UART_NUM_0;
SemaphoreHandle_t output;
QueueHandle_t events;
std::atomic<unsigned> driver_drops{0}, rx_errors{0};
unsigned current_baud = 115200;
int write(const char* data, size_t size) {
    xSemaphoreTake(output, portMAX_DELAY);
    int n = uart_write_bytes(port, data, size);
    xSemaphoreGive(output);
    if (n != int(size)) ++driver_drops;
    return n;
}
void uart_events(void*) {
    uart_event_t event;
    for (;;) if (xQueueReceive(events, &event, portMAX_DELAY)) {
        if (event.type == UART_FIFO_OVF || event.type == UART_BUFFER_FULL ||
            event.type == UART_FRAME_ERR || event.type == UART_PARITY_ERR) ++rx_errors;
    }
}
struct Drain {
    RingbufHandle_t ring;
    std::atomic<bool> stop{false}, done{false};
    std::atomic<unsigned> lines{0};
};
void drain(void* arg) {
    auto& d = *static_cast<Drain*>(arg);
    for (;;) {
        size_t n;
        auto* p = static_cast<char*>(xRingbufferReceive(d.ring, &n, pdMS_TO_TICKS(10)));
        if (p) { write(p, n); ++d.lines; vRingbufferReturnItem(d.ring, p); }
        else if (d.stop) break;
    }
    d.done = true;
    vTaskDelete(nullptr);
}
}
uint32_t crc32(const void* data, size_t size) {
    uint32_t crc = ~uint32_t(0);
    auto* p = static_cast<const uint8_t*>(data);
    for (size_t i = 0; i < size; ++i) {
        crc ^= p[i];
        for (int j = 0; j < 8; ++j) crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1)));
    }
    return ~crc;
}
void console_init() {
    output = xSemaphoreCreateMutex();
    ESP_ERROR_CHECK(output ? ESP_OK : ESP_ERR_NO_MEM);
    uart_config_t config{};
    config.baud_rate = current_baud; config.data_bits = UART_DATA_8_BITS;
    config.parity = UART_PARITY_DISABLE; config.stop_bits = UART_STOP_BITS_1;
    config.flow_ctrl = UART_HW_FLOWCTRL_DISABLE; config.source_clk = UART_SCLK_DEFAULT;
    ESP_ERROR_CHECK(uart_param_config(port, &config));
    ESP_ERROR_CHECK(uart_set_pin(port, 37, 38, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
    ESP_ERROR_CHECK(uart_driver_install(port, 8192, 16384, 32, &events, 0));
    uart_vfs_dev_use_driver(port);
    ESP_ERROR_CHECK(xTaskCreate(uart_events, "uart-events", 3072, nullptr, 12, nullptr) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
}
int print(const char* format, ...) {
    xSemaphoreTake(output, portMAX_DELAY);
    static char text[4096];
    va_list args; va_start(args, format);
    int n = vsnprintf(text, sizeof text, format, args); va_end(args);
    if (n < 0 || n >= int(sizeof text)) { ++driver_drops; xSemaphoreGive(output); return -1; }
    int written = uart_write_bytes(port, text, size_t(n));
    if (written != n) ++driver_drops;
    xSemaphoreGive(output);
    return written;
}
int read(char* data, size_t size) { return uart_read_bytes(port, data, size, pdMS_TO_TICKS(100)); }
void baud(unsigned rate) {
    if (rate != 115200 && rate != 921600 && rate != 1500000 && rate != 2000000) {
        print("ERR baud\n"); return;
    }
    print("BAUD rate=%u old=%u\n", rate, current_baud);
    auto rc = uart_wait_tx_done(port, pdMS_TO_TICKS(3000));
    if (rc != ESP_OK) { print("ERR baud tx rc=%d\n", rc); return; }
    vTaskDelay(pdMS_TO_TICKS(200));
    ESP_ERROR_CHECK(uart_set_baudrate(port, rate)); current_baud = rate;
    print("BAUD READY rate=%u\n", rate);
}
void stream(bool buffered, unsigned seconds, unsigned line_bytes) {
    if (!seconds || seconds > 120 || line_bytes < 64 || line_bytes > 2048) {
        print("ERR stream seconds=1..120 line_bytes=64..2048\n"); return;
    }
    Drain d{};
    if (buffered) {
        d.ring = xRingbufferCreate(64 * 1024, RINGBUF_TYPE_NOSPLIT);
        if (!d.ring) { print("ERR ring allocation\n"); return; }
        if (xTaskCreate(drain, "ring-drain", 4096, &d, 8, nullptr) != pdPASS) {
            vRingbufferDelete(d.ring); print("ERR ring task\n"); return;
        }
    }
    print("%s BEGIN seconds=%u line_bytes=%u\n", buffered ? "RING" : "FLOOD", seconds, line_bytes);
    auto start = esp_timer_get_time();
    unsigned lines = 0, drops = 0, prior = driver_drops.load();
    // 8N1 is 10 UART bits per byte; produce at 1.2 * baud/10 bytes/s.
    const uint64_t producer_bytes_per_second = buffered ? uint64_t(current_baud) * 3 / 25 : 0;
    const int64_t period_us = buffered ? (uint64_t(line_bytes) * 1000000 + producer_bytes_per_second - 1) / producer_bytes_per_second : 0;
    int64_t next_line_us = start;
    std::vector<char> line(line_bytes + 1);
    constexpr size_t prefix = 22; // F/R + space + 10 digit seq + space + 8 hex CRC + space
    while (esp_timer_get_time() - start < int64_t(seconds) * 1000000) {
        size_t payload = line_bytes - prefix - 1;
        memset(line.data() + prefix, 'a' + lines % 26, payload);
        uint32_t crc = crc32(line.data() + prefix, payload);
        // snprintf's NUL is overwritten by the payload's first byte.
        snprintf(line.data(), prefix + 1, "%c %010u %08lx ", buffered ? 'R' : 'F', lines, (unsigned long)crc);
        line[prefix] = 'a' + lines % 26;
        line[line_bytes - 1] = '\n';
        if (buffered) {
            if (xRingbufferSend(d.ring, line.data(), line_bytes, 0) != pdTRUE) ++drops;
            next_line_us += period_us;
            auto delay_us = next_line_us - esp_timer_get_time();
            if (delay_us > 0) vTaskDelay(pdMS_TO_TICKS((delay_us + 999) / 1000));
            else next_line_us = esp_timer_get_time(); // Do not catch up with a burst.
        } else write(line.data(), line_bytes);
        ++lines;
    }
    auto produced_us = esp_timer_get_time() - start;
    if (buffered) {
        d.stop = true;
        while (!d.done) vTaskDelay(1);
        vRingbufferDelete(d.ring);
    }
    auto tx_wait_rc = uart_wait_tx_done(port, pdMS_TO_TICKS(5000));
    if (tx_wait_rc != ESP_OK) print("ERR stream tx rc=%d\n", tx_wait_rc);
    print("%s END lines=%u bytes=%llu elapsed_us=%lld produced_us=%lld driver_drops=%u ring_drops=%u sent_lines=%u rx_errors=%u tx_wait_rc=%d producer_bytes_per_second=%llu\n",
          buffered ? "RING" : "FLOOD", lines, (unsigned long long)lines * line_bytes,
          (long long)(esp_timer_get_time() - start), (long long)produced_us,
          driver_drops.load() - prior, drops, buffered ? d.lines.load() : lines, rx_errors.load(), tx_wait_rc, (unsigned long long)producer_bytes_per_second);
}
}
