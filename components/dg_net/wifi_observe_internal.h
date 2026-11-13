#pragma once
#include "esp_wifi.h"
#ifdef __cplusplus
extern "C" {
#endif
// Boot-only opt-in. Failure disables observation without altering networking.
esp_err_t dg_wifi_observe_start(const wifi_init_config_t* configured);
#ifdef __cplusplus
}
#endif
