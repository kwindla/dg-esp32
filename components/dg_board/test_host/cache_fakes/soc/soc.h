#pragma once
#include <cstdint>
uint32_t cache_reg_read(unsigned);void cache_reg_write(unsigned,uint32_t);
#define REG_READ(r) cache_reg_read(r)
#define REG_WRITE(r,v) cache_reg_write(r,v)
