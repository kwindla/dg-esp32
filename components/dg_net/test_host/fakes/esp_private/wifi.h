#pragma once
#include <cstdint>
using wifi_tx_done_cb_t=void(*)(uint8_t,uint8_t*,uint16_t*,bool);
int esp_wifi_set_tx_done_cb(wifi_tx_done_cb_t);
