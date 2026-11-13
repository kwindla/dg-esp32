#include "console_backend.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
namespace dg_console {
namespace {
void init_driver() {
    usb_serial_jtag_driver_config_t console{};
    console.tx_buffer_size=4096;console.rx_buffer_size=1024;
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&console));
    usb_serial_jtag_vfs_use_driver();
}
int send(const char* text,size_t size) {return usb_serial_jtag_write_bytes(text,size,0);}
void wait() {vTaskDelay(1);}
RxStats rx_stats() {return {};}
}
const Backend usb_serial_jtag_backend{init_driver,usb_serial_jtag_is_connected,send,wait,rx_stats};
}
