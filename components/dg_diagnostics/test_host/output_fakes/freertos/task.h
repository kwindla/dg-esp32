#pragma once
#include "FreeRTOS.h"
void vTaskDelay(unsigned);
int xTaskCreatePinnedToCoreWithCaps(void(*)(void*),const char*,unsigned,void*,unsigned,TaskHandle_t*,int,unsigned);
