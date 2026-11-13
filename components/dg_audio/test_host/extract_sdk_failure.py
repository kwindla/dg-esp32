"""Compile the installed IDF failure routine verbatim in the host death test.

This exercises the abort decision, not a host port of the proprietary DSP or
the complete IDF allocator. No SDK/managed file is changed.
"""
from pathlib import Path
import sys

source=Path(sys.argv[1]).read_text()
begin=source.index('HEAP_IRAM_ATTR static void hex_to_str(')
end=source.index('\nesp_err_t heap_caps_register_failed_alloc_callback(',begin)
block=source[begin:end]
assert 'HEAP_IRAM_ATTR NOINLINE_ATTR static void heap_caps_alloc_failed(' in block
assert '#ifdef CONFIG_HEAP_ABORT_WHEN_ALLOCATION_FAILS' in block
assert 'esp_system_abort(buf);' in block
prefix='''// Generated from the installed SDK; do not edit.
#include "sdkconfig.h"
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <cstdio>
#include <cstdlib>
#define HEAP_IRAM_ATTR
#define NOINLINE_ATTR
using esp_alloc_failed_hook_t=void (*)(size_t,uint32_t,const char*);
static bool callback_seen=false;
static void record_failure(size_t,uint32_t,const char*) {callback_seen=true;}
static esp_alloc_failed_hook_t alloc_failed_callback=record_failure;
[[noreturn]] static void esp_system_abort(const char*) {
    std::fputs(callback_seen?"SDK_ALLOCATION_FAILURE\\n":"MISSING_FAILURE_CALLBACK\\n",stderr);
    std::abort();
}
#ifdef CONFIG_HEAP_ABORT_WHEN_ALLOCATION_FAILS
'''
suffix='''
void aec_test_allocation_failed(size_t bytes,uint32_t caps) {
    heap_caps_alloc_failed(bytes,caps,"aec_factory_fault_injection");
}
'''
Path(sys.argv[2]).write_text(prefix+block+suffix)
