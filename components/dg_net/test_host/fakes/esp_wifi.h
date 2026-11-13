#pragma once
#include <cstdint>
using esp_err_t=int;
constexpr int ESP_OK=0,ESP_ERR_NO_MEM=0x101,ESP_ERR_INVALID_ARG=0x102,
 ESP_ERR_INVALID_STATE=0x103,ESP_ERR_NOT_SUPPORTED=0x106;
constexpr int WIFI_IF_STA=0,WIFI_IF_AP=1;
enum wifi_ps_type_t {WIFI_PS_NONE,WIFI_PS_MIN_MODEM,WIFI_PS_MAX_MODEM};
struct wifi_ap_record_t {int rssi,primary,second;uint8_t bssid[6];};
struct wifi_init_config_t {
 int static_tx_buf_num,cache_tx_buf_num,static_rx_buf_num,dynamic_rx_buf_num;
 int tx_buf_type,ampdu_tx_enable,ampdu_rx_enable,rx_ba_win,wifi_task_core_id;
};
esp_err_t esp_wifi_get_ps(wifi_ps_type_t*);
esp_err_t esp_wifi_sta_get_ap_info(wifi_ap_record_t*);
