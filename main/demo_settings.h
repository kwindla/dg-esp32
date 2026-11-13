#pragma once
#include "esp_err.h"
#include <cstdint>
namespace dg {
struct DemoSettings {
    uint32_t version=1;
    bool configured=false,enabled=false;
    int volume_db=-6;
    char url[256]{};
};
// Load and create the internal-stack NVS writer from app_main, after net/NVS init.
void demo_settings_init();
DemoSettings demo_settings();
esp_err_t demo_settings_save(const DemoSettings& settings);
}
