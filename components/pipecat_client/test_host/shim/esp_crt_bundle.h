#pragma once
#include "esp_http_client.h"
// Host stand-in for the IDF certificate bundle hook; the test records the pointer.
esp_err_t esp_crt_bundle_attach(void* conf);
