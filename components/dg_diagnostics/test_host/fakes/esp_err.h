#pragma once
#include <stdexcept>
#define ESP_OK 0
#define ESP_ERR_NOT_SUPPORTED 0x106
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_NO_MEM 0x101
#define ESP_ERROR_CHECK(call) do {if((call)!=ESP_OK)throw std::runtime_error("ESP_ERROR_CHECK");} while(0)
