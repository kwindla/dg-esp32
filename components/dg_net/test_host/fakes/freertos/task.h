#pragma once
#include "FreeRTOS.h"
int xTaskCreatePinnedToCoreWithCaps(void(*)(void*),const char*,unsigned,void*,unsigned,TaskHandle_t*,int,unsigned);
void vTaskDelay(unsigned);
