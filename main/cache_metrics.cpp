#include "cache_metrics.h"
#include "dg_board/cache.h"
void cache_metrics_enable(bool value){dg_board::cache::enable(value);}
pipecat::esp_peer::StatJson cache_metrics_snapshot(){
    using Json=pipecat::esp_peer::StatJson;
    dg_board::cache::Snapshot row;
    if(!dg_board::cache::read(row))return nullptr;
    Json result={{"cpu_hz",row.cpu_hz},{"apb_hz",row.apb_hz},
        {"mspi_core_clock_register",row.mspi_core},
        {"mspi_flash_clock_register",row.mspi_flash},
        {"mspi_psram_clock_register",row.mspi_psram},
        {"window_us",row.window_us},{"ibus_accesses",row.ibus_accesses},
        {"ibus_misses",row.ibus_misses},
        {"dbus_accesses",row.dbus_accesses},
        {"dbus_flash_misses",row.dbus_flash_misses},
        {"dbus_psram_misses",row.dbus_psram_misses},
        {"overflow_status",row.overflow_status},
        {"dbus_image_start",row.dbus_image_start},
        {"dbus_image_end",row.dbus_image_end},
        {"address_classification","DBUS flash counter = image-rodata virtual pages; PSRAM counter = other cached data. XIP changes physical backing, not this address classification"},
        {"basis","Shared hardware cache counters; reset after every PERF; sequential reads"}};
    dg_board::cache::reset();return result;
}
