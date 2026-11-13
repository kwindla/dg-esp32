#pragma once
#include <cstddef>
#include "dg_console.h"
namespace dg_console {
struct Backend {
    void (*init)();
    bool (*connected)();
    int (*send)(const char*,size_t);
    void (*wait)();
    RxStats (*rx_stats)();
};
const Backend& backend();
extern const Backend usb_serial_jtag_backend,uart_backend;
}
