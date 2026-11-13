#pragma once
constexpr int ESP_OK=0;
struct esp_pthread_cfg_t {unsigned stack_size=1024;const char* thread_name="default";};
esp_pthread_cfg_t esp_pthread_get_default_config();
int esp_pthread_get_cfg(esp_pthread_cfg_t*);
int esp_pthread_set_cfg(const esp_pthread_cfg_t*);
