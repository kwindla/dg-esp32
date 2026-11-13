#pragma once
#include "FreeRTOS.h"
#include <cstddef>
#include <cstdint>
struct FakeRing;
using RingbufHandle_t=FakeRing*;
struct StaticRingbuffer_t {};
constexpr unsigned RINGBUF_TYPE_NOSPLIT=0;
RingbufHandle_t xRingbufferCreateStatic(size_t,unsigned,uint8_t*,StaticRingbuffer_t*);
int xRingbufferSend(RingbufHandle_t,const void*,size_t,unsigned);
void* xRingbufferReceive(RingbufHandle_t,size_t*,unsigned);
void vRingbufferReturnItem(RingbufHandle_t,void*);
