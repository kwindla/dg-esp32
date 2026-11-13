#include "dg_board/cache.h"
#include "esp_timer.h"
#include "soc/soc.h"
#include "soc/extmem_reg.h"
#include "soc/spi_mem_reg.h"
#include "esp_private/esp_clk.h"
#include <cstdint>
extern "C" {extern char _rodata_reserved_start[],_rodata_reserved_end[];}
namespace dg_board::cache {
namespace {
bool enabled=false;
int64_t began=0;
constexpr uint32_t overflow_mask=EXTMEM_DBUS_ACS_SPIRAM_MISS_CNT_OVF_ST |
    EXTMEM_DBUS_ACS_FLASH_MISS_CNT_OVF_ST | EXTMEM_DBUS_ACS_CNT_OVF_ST |
    EXTMEM_IBUS_ACS_MISS_CNT_OVF_ST | EXTMEM_IBUS_ACS_CNT_OVF_ST;
void clear() {
    REG_WRITE(EXTMEM_CACHE_ACS_CNT_CLR_REG,EXTMEM_ICACHE_ACS_CNT_CLR|EXTMEM_DCACHE_ACS_CNT_CLR);
    // Counter-overflow status is sticky and independent of interrupt enable.
    REG_WRITE(EXTMEM_CACHE_ILG_INT_CLR_REG,EXTMEM_IBUS_CNT_OVF_INT_CLR|EXTMEM_DBUS_CNT_OVF_INT_CLR);
    began=esp_timer_get_time();
}
}
void enable(bool value) {
    enabled=value;
    if(!value)return;
    // Count all external cached virtual addresses. These range registers
    // affect statistics only, not cache mapping, size, lines or replacement.
    REG_WRITE(EXTMEM_IBUS_TO_FLASH_START_VADDR_REG,0x42000000);
    REG_WRITE(EXTMEM_IBUS_TO_FLASH_END_VADDR_REG,0x43ffffff);
    // The hardware classifies DBUS misses by virtual address. Cover only
    // mapped image-rodata pages; the remaining addresses count dynamic PSRAM.
    // With XIP both classes physically use PSRAM, so this is address provenance.
    const auto begin=reinterpret_cast<uintptr_t>(_rodata_reserved_start)&~uintptr_t(65535);
    const auto end=(reinterpret_cast<uintptr_t>(_rodata_reserved_end)+65535)&~uintptr_t(65535);
    REG_WRITE(EXTMEM_DBUS_TO_FLASH_START_VADDR_REG,begin);
    REG_WRITE(EXTMEM_DBUS_TO_FLASH_END_VADDR_REG,end-1);
    clear();
}
bool read(Snapshot& result) {
    if(!enabled)return false;
    const auto end=esp_timer_get_time();
    result={uint32_t(esp_clk_cpu_freq()),uint32_t(esp_clk_apb_freq()),
        REG_READ(SPI_MEM_CORE_CLK_SEL_REG(0)),REG_READ(SPI_MEM_CLOCK_REG(0)),REG_READ(SPI_MEM_SRAM_CLK_REG(0)),end-began,
        REG_READ(EXTMEM_IBUS_ACS_CNT_REG),REG_READ(EXTMEM_IBUS_ACS_MISS_CNT_REG),REG_READ(EXTMEM_DBUS_ACS_CNT_REG),
        REG_READ(EXTMEM_DBUS_ACS_FLASH_MISS_CNT_REG),REG_READ(EXTMEM_DBUS_ACS_SPIRAM_MISS_CNT_REG),
        REG_READ(EXTMEM_CACHE_ILG_INT_ST_REG)&overflow_mask,
        REG_READ(EXTMEM_DBUS_TO_FLASH_START_VADDR_REG),REG_READ(EXTMEM_DBUS_TO_FLASH_END_VADDR_REG)};
    return true;
}
void reset(){if(enabled)clear();}
}
