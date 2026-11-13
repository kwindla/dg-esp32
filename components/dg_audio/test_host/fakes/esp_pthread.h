#pragma once
#ifndef ESP_OK
#define ESP_OK 0
#endif
struct esp_pthread_cfg_t {unsigned stack_size=1024,prio=1,stack_alloc_caps=0;int pin_to_core=0;const char* thread_name="default";};
inline esp_pthread_cfg_t esp_pthread_get_default_config() {return {};}
inline int esp_pthread_get_cfg(esp_pthread_cfg_t*) {return -1;}
inline int esp_pthread_set_cfg(const esp_pthread_cfg_t*) {return 0;}
