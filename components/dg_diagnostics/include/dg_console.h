#pragma once
#include <cstddef>
#include <cstdarg>
namespace dg_console {
// disconnected counts SOF-false observations, not dropped records.
struct RxStats {unsigned fifo_overflows,buffer_full,frame_errors,parity_errors;};
struct Stats {unsigned queued, disconnected, full, transport_drops;RxStats rx{};unsigned ack_pending=0;};
// Install the board's driver-backed console and output ring. Log producers never wait for transport.
void init();
bool ready();
bool connected();
// Called only by dump producers, outside stdio/audio locks.
void pace_dump(size_t bytes,unsigned usb_ms=8);
bool write(const char* text, size_t bytes);
// Console owner only, once per command (or early OK followed by ERR). UART
// ACKs have reserved space, follow earlier response records, and retain partial-write progress until retired.
// Poll ack_pending before consuming another command; admission only copies under the stdout lock.
void acknowledge(const char* text,size_t bytes);
bool ack_pending();
int vprint(const char* format, va_list args);
Stats stats();
}
