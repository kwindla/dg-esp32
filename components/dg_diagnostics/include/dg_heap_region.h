#pragma once
#include "soc/soc.h"
#include "sdkconfig.h"
#include <cstdint>
namespace dg_console {
// Names/indexes retain the diagnostic wire schema. Boundaries come from the
// target memory map, including the ROM boot stack recycled after app_main.
inline unsigned heap_region(uintptr_t start,bool internal,bool reserve) {
    if(!internal)return 5;
    if((start>=SOC_RTC_DRAM_LOW && start<SOC_RTC_DRAM_HIGH) ||
       (start>=SOC_RTC_DATA_LOW && start<SOC_RTC_DATA_HIGH))return 4;
    if(start>=SOC_DIRAM_DRAM_HIGH && start<SOC_DRAM_HIGH)return 2;
    if(reserve)return 3;
#if CONFIG_IDF_TARGET_ESP32P4
    // Match IDF heap/port/esp32p4/memory_layout.c, not the older ROM
    // reservation boundary: recycled L2 startup data can cross 0x4ff40000.
#if CONFIG_ESP32P4_SELECTS_REV_LESS_V3
    constexpr auto rom_start=SOC_ROM_STACK_START;
    constexpr auto rom_end=SOC_DRAM_HIGH-CONFIG_CACHE_L2_CACHE_SIZE;
#else
    constexpr auto rom_start=SOC_ROM_STACK_START_REV2;
    constexpr auto rom_end=SOC_DRAM_HIGH;
#endif
#else
    constexpr auto rom_start=SOC_ROM_STACK_START;
#ifdef SOC_DIRAM_ROM_RESERVE_HIGH
    constexpr auto rom_end=SOC_DIRAM_ROM_RESERVE_HIGH;
#else
    constexpr auto rom_end=SOC_DIRAM_DRAM_HIGH;
#endif
#endif
    if(start>=rom_start-SOC_ROM_STACK_SIZE && start<rom_end)return 1;
    return 0;
}
}
