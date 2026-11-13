#pragma once
#include "esp_err.h"
#include "dg_touch_gesture.h"
namespace dg_board {
struct TouchPoint {uint16_t x=0,y=0;uint8_t contacts=0;};
struct TouchStatus {bool initialized=false;uint32_t interrupts=0,reads=0,read_errors=0,coalesced=0,injected=0,injected_ignored=0;TouchCounts gestures;};
namespace touch {
esp_err_t init();
// Bounded controller read, no gesture/counter update. Native coordinates;
// caller validates contact count/bounds. Goodix returns NOT_FINISHED when no
// fresh sample is ready. Never call from an ISR.
esp_err_t read_raw(TouchPoint& point);
// One non-audio owner polls every 10-20ms. Polled boards read at most 50Hz;
// each I2C transaction and the P4 bus-mutex wait have a 10ms timeout.
bool poll(uint32_t now_ms,bool accept_reconnect);
bool inject_double_tap()noexcept;
TouchStatus status();
}
}
