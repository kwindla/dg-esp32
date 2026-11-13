#pragma once
#include <cstdlib>
using esp_err_t=int;
constexpr int ESP_OK=0,ESP_ERR_INVALID_STATE=1,ESP_ERR_NOT_SUPPORTED=2,ESP_ERR_TIMEOUT=3;
#define ESP_ERROR_CHECK(expression) do {if((expression)!=ESP_OK)std::abort();} while(false)

constexpr esp_err_t ESP_ERR_INVALID_ARG=0x102,ESP_ERR_NOT_FINISHED=0x10c,ESP_ERR_INVALID_RESPONSE=0x108;
