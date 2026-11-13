#pragma once
#include "FreeRTOS.h"
void vTaskDelay(unsigned);
TaskHandle_t xTaskGetCurrentTaskHandle();
void xTaskNotifyGive(TaskHandle_t);
unsigned ulTaskNotifyTake(unsigned,unsigned);
int xTaskCreate(void(*)(void*),const char*,unsigned,void*,unsigned,TaskHandle_t*);
