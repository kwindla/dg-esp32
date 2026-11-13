#include "esp_app_desc.h"
#include "dg_diagnostics.h"
#include "dg_console.h"
#include "dg_display.h"
#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "demo_settings.h"
#include "performance.h"
#include "soc/soc_caps.h"
#if SOC_WIFI_SUPPORTED
#include "esp_phy_init.h"
#endif
#include "dg_net.h"
#include "sdkconfig.h"
#if CONFIG_DG_DUO
#include "duo_settings.h"
#endif
// Preserve legacy ESP_ERROR_CHECK source-line constants in duo-off images.
#line 17
#include "esp_log.h"
#include "esp_pthread.h"
#include "esp_heap_caps.h"
#include <thread>
#include <cstdio>
#include <cstring>
void run_console();
void log_heap(const char* phase);
static const char* TAG="dg-esp32";
extern "C" void app_main() {
    dg_board::early_board_init();
    performance_init();
    dg_console::init();
    const auto* desc = esp_app_get_description(); char hash[65];
    for (unsigned i = 0; i < 32; ++i) std::sprintf(hash + i * 2, "%02x", desc->app_elf_sha256[i]);
    dg_board::info().log_audio_boot(hash);
    ESP_LOGI(TAG,"DG_BOOT board=%s chip=%s idf=%s",dg_board::info().name,dg_board::info().chip,desc->idf_ver);
    dg_diagnostics_init(); log_heap("boot"); dg_diagnostics_flush();
    // NVS initialization can disable flash cache: never run it on PSRAM.
    if (*CONFIG_DG_WIFI_SSID) ESP_ERROR_CHECK(dg_net_init(CONFIG_DG_WIFI_SSID, CONFIG_DG_WIFI_PASSWORD));
    dg::demo_settings_init();
#if CONFIG_DG_DUO
    dg::duo_settings_init();
#endif
#line 38
#if SOC_WIFI_SUPPORTED
    if (*CONFIG_DG_WIFI_SSID) {
        auto* calibration=static_cast<esp_phy_calibration_data_t*>(heap_caps_malloc(sizeof(esp_phy_calibration_data_t),MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        if(!calibration) {ESP_LOGE(TAG,"PHYCAL no memory for calibration data");abort();}
        auto status=esp_phy_load_cal_data_from_nvs(calibration);
#if CONFIG_DG_VERIFY_PHY_STORAGE
        ESP_ERROR_CHECK(status);
        auto stored=esp_phy_store_cal_data_to_nvs(calibration);
        auto* verify=static_cast<esp_phy_calibration_data_t*>(heap_caps_malloc(sizeof(esp_phy_calibration_data_t),MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        if(!verify) {ESP_LOGE(TAG,"PHYCAL no memory for the verification copy");abort();}
        auto reread=esp_phy_load_cal_data_from_nvs(verify);
        bool equal=reread==ESP_OK && std::memcmp(calibration,verify,sizeof(*verify))==0; free(verify);
        ESP_LOGI(TAG,"PHYCAL stored=%d reread=%d equal=%d",int(stored),int(reread),equal);
        ESP_ERROR_CHECK(stored); ESP_ERROR_CHECK(reread); ESP_ERROR_CHECK(equal?ESP_OK:ESP_FAIL);
#endif
        free(calibration);
        ESP_LOGI(TAG,"PHYCAL loaded=%d bytes=%u",int(status),unsigned(sizeof(esp_phy_calibration_data_t)));
    }
#endif
    ESP_LOGI(TAG, "STACK task=boot free_bytes=%u", unsigned(uxTaskGetStackHighWaterMark(nullptr)));
    auto cfg = esp_pthread_get_default_config();
    cfg.stack_alloc_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
    cfg.pin_to_core = 0; cfg.prio = 4; cfg.stack_size = 18432; cfg.thread_name = "console";
    ESP_ERROR_CHECK(esp_pthread_set_cfg(&cfg)); std::thread(run_console).detach();
}
