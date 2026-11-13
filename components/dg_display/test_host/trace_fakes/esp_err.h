#pragma once
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NO_MEM 0x101
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_NOT_SUPPORTED 0x106
#define BIT(n) (1u<<(n))
#ifndef __containerof
#define __containerof(p,t,m) ((t*)((char*)(p)-offsetof(t,m)))
#endif
#define ESP_ERROR_CHECK(e) assert((e)==ESP_OK)
