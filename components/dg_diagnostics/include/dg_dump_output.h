#pragma once
#include "dg_console.h"
#include <cstring>
namespace dg_console {
// One framing/record line, with the step-6 UART wire budget. usb_ms=0 leaves
// USB framing/control lines unchanged; existing USB PCM pacing stays explicit.
inline void dump_line(const char* text,void (*output)(const char*),unsigned usb_ms=0){
    output(text);pace_dump(std::strlen(text),usb_ms);
}
}
