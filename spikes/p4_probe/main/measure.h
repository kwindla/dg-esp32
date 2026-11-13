#pragma once
#include "console.h"
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
namespace measure {
// CRC covers the complete JSON payload. A missing/corrupt measurement is fatal
// to the host campaign; it must never turn into a fabricated successful row.
inline void emit(const char* format, ...) __attribute__((format(printf,1,2)));
inline void emit(const char* format, ...) {
    char json[768]; va_list args; va_start(args,format);
    int n=vsnprintf(json,sizeof json,format,args);va_end(args);
    if(n<0 || n>=int(sizeof json)) std::abort();
    probe::print("B %s %08lx\n",json,(unsigned long)probe::crc32(json,n));
}
}
