#pragma once
#include "esp_pthread.h"
#include <stdexcept>

namespace dg {
// IDF has no unset API. Restore the caller's explicit config, or the default
// when this task had no config, on every exit including thread launch errors.
class PthreadConfig {
    esp_pthread_cfg_t previous_=esp_pthread_get_default_config();
public:
    PthreadConfig() {esp_pthread_cfg_t cfg{};if(esp_pthread_get_cfg(&cfg)==ESP_OK)previous_=cfg;}
    ~PthreadConfig() {esp_pthread_set_cfg(&previous_);}
    PthreadConfig(const PthreadConfig&)=delete;
    esp_pthread_cfg_t current() const {return previous_;}
    void set(const esp_pthread_cfg_t& cfg) {
        if(esp_pthread_set_cfg(&cfg)!=ESP_OK)throw std::runtime_error("Pthread config failed");
    }
};
}
