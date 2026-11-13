#pragma once
#include <cstdint>
#ifdef ESP_PLATFORM
#include "sdkconfig.h"
#endif
namespace dg_board {
#ifdef ESP_PLATFORM
#if CONFIG_IDF_TARGET_ESP32P4
inline constexpr unsigned DataCacheLine=CONFIG_CACHE_L2_CACHE_LINE_SIZE;
#elif CONFIG_IDF_TARGET_ESP32S3
inline constexpr unsigned DataCacheLine=CONFIG_ESP32S3_DATA_CACHE_LINE_SIZE;
#else
#error Unsupported display target
#endif
#else
inline constexpr unsigned DataCacheLine=64;
#endif
#if CONFIG_IDF_TARGET_ESP32S3
inline constexpr bool native_wifi_observer=true;
#else
inline constexpr bool native_wifi_observer=false;
#endif
namespace cache {
struct Snapshot {
    uint32_t cpu_hz=0,apb_hz=0,mspi_core=0,mspi_flash=0,mspi_psram=0;
    int64_t window_us=0;
    uint32_t ibus_accesses=0,ibus_misses=0,dbus_accesses=0,dbus_flash_misses=0,dbus_psram_misses=0;
    uint32_t overflow_status=0,dbus_image_start=0,dbus_image_end=0;
};
void enable(bool);
bool read(Snapshot&);
// Reset after the caller has finished constructing its diagnostic snapshot.
void reset();
}
}
