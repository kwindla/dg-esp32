#pragma once
#include <cstddef>
#include <cstdint>
namespace probe {
void console_init();
int print(const char* format, ...) __attribute__((format(printf, 1, 2)));
int read(char* data, size_t size);
void baud(unsigned rate);
void stream(bool ring, unsigned seconds, unsigned line_bytes);
uint32_t crc32(const void* data, size_t size);
}
