#pragma once
#define LCD_CMD_SWRESET 0x01
#define LCD_CMD_MADCTL 0x36
#define LCD_CMD_COLMOD 0x3a
#define LCD_CMD_CASET 0x2a
#define LCD_CMD_RASET 0x2b
#define LCD_CMD_RAMWR 0x2c
#define LCD_CMD_INVON 0x21
#define LCD_CMD_INVOFF 0x20
#define LCD_CMD_DISPON 0x29
#define LCD_CMD_DISPOFF 0x28
#define LCD_CMD_BGR_BIT (1<<3)
#define LCD_CMD_MX_BIT (1<<6)
#define LCD_CMD_MY_BIT (1<<7)
#define LCD_CMD_MV_BIT (1<<5)
