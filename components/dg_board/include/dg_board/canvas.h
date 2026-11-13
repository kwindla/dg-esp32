#pragma once
#ifdef ESP_PLATFORM
#include "sdkconfig.h"
#endif
namespace dg_board {
#if defined(CONFIG_DG_BOARD_ECHOEAR) || defined(CONFIG_DG_BOARD_P4XC)
inline constexpr int CanvasWidth=360,CanvasHeight=360,PanelXAlign=1,DmaXAlign=8;
inline constexpr bool DmaYEvenIfRow8=false;
#elif defined(CONFIG_DG_BOARD_WAVESHARE_146) || !defined(ESP_PLATFORM)
// The retired Waveshare retains its native 412px logical canvas (also host default).
inline constexpr int CanvasWidth=412,CanvasHeight=412,PanelXAlign=4,DmaXAlign=4;
inline constexpr bool DmaYEvenIfRow8=true;
#else
#error Unknown board logical canvas
#endif
#if defined(CONFIG_DG_BOARD_P4XC)
// The engine remains 360px; only LogicalAdapter uses these native layout values.
inline constexpr int CanvasScale=2,CanvasOffsetX=40,CanvasOffsetY=40;
#else
inline constexpr int CanvasScale=1,CanvasOffsetX=0,CanvasOffsetY=0;
#endif
}
