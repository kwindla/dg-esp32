#pragma once
#include "FreeRTOS.h"
struct StaticSemaphore_t {int token;};
using SemaphoreHandle_t=StaticSemaphore_t*;
SemaphoreHandle_t xSemaphoreCreateMutexStatic(StaticSemaphore_t*);
int xSemaphoreTake(SemaphoreHandle_t,TickType_t);
int xSemaphoreGive(SemaphoreHandle_t);
