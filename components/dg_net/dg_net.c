#include "dg_net.h"
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "wifi_observe_internal.h"
#if CONFIG_IDF_TARGET_ESP32P4
void dg_net_hosted_version(void);
#endif
static EventGroupHandle_t events;
static esp_netif_t *netif;
static char address[16];
static portMUX_TYPE guard = portMUX_INITIALIZER_UNLOCKED;
static int64_t retry_deadline;
static dg_net_disconnect_cb disconnect_cb;
static void *disconnect_ctx;
static dg_net_ip_cb ip_cb;
static void *ip_ctx;
static uint32_t previous_ip;
static void event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg;
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        portENTER_CRITICAL(&guard);
        retry_deadline = 0;
        uint32_t ip = ((ip_event_got_ip_t *)data)->ip_info.ip.addr;
        bool changed = previous_ip && previous_ip != ip;
        previous_ip = ip;
        dg_net_ip_cb cb = ip_cb; void *ctx = ip_ctx;
        portEXIT_CRITICAL(&guard);
        if (cb) cb(changed, ctx);
        xEventGroupSetBits(events, 1);
        wifi_ap_record_t ap;
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK)
            ESP_LOGI("dg-net", "WiFi associated RSSI=%d channel=%u", ap.rssi, ap.primary);
    }
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(events, 1);
        portENTER_CRITICAL(&guard);
        bool retry = retry_deadline > esp_timer_get_time();
        dg_net_disconnect_cb cb = disconnect_cb;
        void *ctx = disconnect_ctx;
        portEXIT_CRITICAL(&guard);
        const wifi_event_sta_disconnected_t *d = data;
        ESP_LOGW("dg-net", "WiFi disconnected reason=%u rssi=%d", (unsigned)d->reason, d->rssi);
        if (cb) cb(d->reason, ctx);
        if (retry) esp_wifi_connect();
    }
}
esp_err_t dg_net_init(const char *ssid, const char *password) {
    if (events) return ESP_ERR_INVALID_STATE;
    if (!ssid || !*ssid || !password || strlen(ssid)>32 || strlen(password)>64) return ESP_ERR_INVALID_ARG;
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        // The default partition also holds the demo and duo settings. Erasing
        // it is the only way back to a working Wi-Fi stack here, so say that
        // it happens and why: a device that lost its configured URL this way
        // otherwise only shows up as "not configured".
        ESP_LOGE("dg-net", "NVS_RECOVERY erasing the default partition reason=%s; demo and duo settings are reset",
                 err == ESP_ERR_NVS_NO_FREE_PAGES ? "no-free-pages" : "new-version");
        ESP_ERROR_CHECK(nvs_flash_erase()); err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    netif = esp_netif_create_default_wifi_sta();
    events = xEventGroupCreate();
    if (!netif || !events) return ESP_ERR_NO_MEM;
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, event, NULL));
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
#if CONFIG_IDF_TARGET_ESP32P4
    dg_net_hosted_version();
#endif
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    wifi_config_t cfg = {0};
    memcpy(cfg.sta.ssid, ssid, strlen(ssid));
    memcpy(cfg.sta.password, password, strlen(password));
    // The bench SSID has multiple APs. Fast scan may stop at a weaker AP;
    // inspect the full scan and prefer signal strength before associating.
    cfg.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    cfg.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
    cfg.sta.failure_retry_cnt = 1;
    if (*password) cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    cfg.sta.pmf_cfg.capable = true;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    const esp_err_t ps_error=esp_wifi_set_ps(WIFI_PS_NONE);
    if(ps_error==ESP_OK)(void)dg_wifi_observe_start(&init);
    return ps_error;
}
esp_err_t dg_net_wifi_connect(unsigned timeout_ms) {
    if (!events) return ESP_ERR_INVALID_STATE;
    if (xEventGroupGetBits(events) & 1) return ESP_OK;
    portENTER_CRITICAL(&guard);
    retry_deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    portEXIT_CRITICAL(&guard);
    esp_err_t err = esp_wifi_connect();
    if (err == ESP_OK)
        err = (xEventGroupWaitBits(events, 1, pdFALSE, pdTRUE, pdMS_TO_TICKS(timeout_ms)) & 1) ? ESP_OK : ESP_ERR_TIMEOUT;
    portENTER_CRITICAL(&guard);
    retry_deadline = 0;
    portEXIT_CRITICAL(&guard);
    return err;
}
void dg_net_set_disconnect_callback(dg_net_disconnect_cb cb, void *ctx) {
    portENTER_CRITICAL(&guard);
    disconnect_cb = cb;
    disconnect_ctx = ctx;
    portEXIT_CRITICAL(&guard);
}
esp_err_t dg_net_ip_info(esp_netif_ip_info_t *info) {
    return netif ? esp_netif_get_ip_info(netif, info) : ESP_ERR_INVALID_STATE;
}
const char *dg_net_ip(void) {
    esp_netif_ip_info_t info;
    if (esp_netif_get_ip_info(netif, &info) != ESP_OK) return "";
    esp_ip4addr_ntoa(&info.ip, address, sizeof(address));
    return address;
}

void dg_net_set_ip_callback(dg_net_ip_cb cb, void *ctx) {
    portENTER_CRITICAL(&guard); ip_cb = cb; ip_ctx = ctx; portEXIT_CRITICAL(&guard);
}
