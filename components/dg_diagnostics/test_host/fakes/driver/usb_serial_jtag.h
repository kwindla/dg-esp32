#pragma once
#include "esp_err.h"
#include <cstddef>
struct usb_serial_jtag_driver_config_t {size_t tx_buffer_size,rx_buffer_size;};
int usb_serial_jtag_driver_install(const usb_serial_jtag_driver_config_t*);
bool usb_serial_jtag_is_connected();
int usb_serial_jtag_write_bytes(const char*,size_t,unsigned);
