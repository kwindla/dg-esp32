#pragma once
#include "FreeRTOS.h"
void vTaskDelay(TickType_t ms);
inline unsigned uxTaskGetStackHighWaterMark(void*) {return 10000;}

using TaskHandle_t=void*;
inline TaskHandle_t xTaskGetCurrentTaskHandle() {return reinterpret_cast<void*>(1);}
inline void xTaskNotifyGive(TaskHandle_t) {}
inline unsigned ulTaskNotifyTake(bool,TickType_t ms) {vTaskDelay(ms);return 0;}
