#pragma once
#include "esp_err.h"
struct esp_pthread_cfg_t {unsigned stack_size=1024,stack_alloc_caps=0;int pin_to_core=-1,prio=1;const char* thread_name="host";};
esp_pthread_cfg_t esp_pthread_get_default_config();
int esp_pthread_get_cfg(esp_pthread_cfg_t*);
int esp_pthread_set_cfg(const esp_pthread_cfg_t*);
