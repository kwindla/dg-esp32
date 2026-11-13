#pragma once
#include <cstdio>
#define ESP_LOGI(tag,format,...) do{(void)(tag);(void)std::snprintf(nullptr,0,format,##__VA_ARGS__);}while(0)
#define ESP_LOGE ESP_LOGI
