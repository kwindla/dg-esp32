#pragma once
#include "esp_err.h"
#include "esp_log.h"
#define ESP_RETURN_ON_FALSE(test,code,...) do{if(!(test))return (code);}while(0)
#define ESP_RETURN_ON_ERROR(expression,...) do{esp_err_t status=(expression);if(status!=ESP_OK)return status;}while(0)
#define ESP_GOTO_ON_FALSE(test,code,label,...) do{if(!(test)){ret=(code);goto label;}}while(0)
#define ESP_GOTO_ON_ERROR(expression,label,...) do{ret=(expression);if(ret!=ESP_OK)goto label;}while(0)
