#pragma once
#include "FreeRTOS.h"
using TaskHandle_t=void*;
unsigned ulTaskNotifyTake(int,TickType_t);
int xTaskCreatePinnedToCore(void(*)(void*),const char*,unsigned,void*,unsigned,TaskHandle_t*,int);
void xTaskNotifyGive(TaskHandle_t);
