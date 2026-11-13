#pragma once
#include <cstddef>
#include <cstdint>
#include "i2s_common.h"
constexpr int ESP_OK=0,ESP_ERR_TIMEOUT=0x107;
int i2s_channel_read(i2s_channel_obj_t*,void*,size_t,size_t*,uint32_t);
