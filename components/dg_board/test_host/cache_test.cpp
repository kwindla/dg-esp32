#include "dg_board/cache.h"
#include "../../../main/cache_metrics.h"
#include "soc/extmem_reg.h"
#include <gtest/gtest.h>
#include <array>
#include <cstdlib>
#include <vector>
namespace {
std::array<uint32_t,32> registers{};
std::vector<unsigned> writes;
int64_t now=100;
bool constructing=false;size_t clear_before=0;
}
extern "C" {char _rodata_reserved_start[1],_rodata_reserved_end[1];}
int64_t esp_timer_get_time(){return now;}
int esp_clk_cpu_freq(){return 240000000;}
int esp_clk_apb_freq(){return 80000000;}
uint32_t cache_reg_read(unsigned reg){return registers[reg];}
void cache_reg_write(unsigned reg,uint32_t value){registers[reg]=value;writes.push_back(reg);}
namespace pipecat::esp_peer {
void* stat_allocate(size_t n){if(constructing){EXPECT_EQ(writes.size(),clear_before);}return std::malloc(n);}
void stat_free(void* p){std::free(p);}
}
TEST(CacheMetrics,PreservesRegisterFieldsAndResetsOnlyAfterJsonConstruction){
    cache_metrics_enable(false);writes.clear();EXPECT_TRUE(cache_metrics_snapshot().is_null());EXPECT_TRUE(writes.empty());
    now=100;cache_metrics_enable(true);
    ASSERT_EQ(writes.size(),6u);
    EXPECT_EQ(registers[EXTMEM_IBUS_TO_FLASH_START_VADDR_REG],0x42000000u);
    EXPECT_EQ(registers[EXTMEM_IBUS_TO_FLASH_END_VADDR_REG],0x43ffffffu);
    for(unsigned i=7;i<16;++i)registers[i]=i*10;
    clear_before=writes.size();constructing=true;now=250;
    const auto row=cache_metrics_snapshot();constructing=false;
    EXPECT_EQ(row["window_us"],150);EXPECT_EQ(row["cpu_hz"],240000000);EXPECT_EQ(row["apb_hz"],80000000);
    EXPECT_EQ(row["mspi_core_clock_register"],130);EXPECT_EQ(row["mspi_flash_clock_register"],140);EXPECT_EQ(row["mspi_psram_clock_register"],150);
    EXPECT_EQ(row["ibus_accesses"],70);EXPECT_EQ(row["ibus_misses"],80);EXPECT_EQ(row["dbus_accesses"],90);
    EXPECT_EQ(row["dbus_flash_misses"],100);EXPECT_EQ(row["dbus_psram_misses"],110);EXPECT_EQ(row["overflow_status"],120&31);
    EXPECT_EQ(row["dbus_image_start"],registers[EXTMEM_DBUS_TO_FLASH_START_VADDR_REG]);
    EXPECT_EQ(row["dbus_image_end"],registers[EXTMEM_DBUS_TO_FLASH_END_VADDR_REG]);
    EXPECT_EQ(writes.size(),clear_before+2);EXPECT_EQ(writes[writes.size()-2],EXTMEM_CACHE_ACS_CNT_CLR_REG);
    EXPECT_EQ(writes.back(),EXTMEM_CACHE_ILG_INT_CLR_REG);
    now=300;EXPECT_EQ(cache_metrics_snapshot()["window_us"],50);
    cache_metrics_enable(false);writes.clear();EXPECT_TRUE(cache_metrics_snapshot().is_null());EXPECT_TRUE(writes.empty());
}
