#pragma once
#include "dg_board/canvas.h"
#include "dg_board/cache.h"
#ifdef ESP_PLATFORM
#include "sdkconfig.h"
#endif
namespace dg_display {
// Compatibility names describe the LOGICAL canvas. Native geometry is backend info().
inline constexpr int PanelWidth=dg_board::CanvasWidth,PanelHeight=dg_board::CanvasHeight;
inline constexpr int PanelXAlign=dg_board::PanelXAlign,DmaXAlign=dg_board::DmaXAlign;
inline constexpr bool DmaYEvenIfRow8=dg_board::DmaYEvenIfRow8;
inline constexpr unsigned StagingAlignment=64,StagingBytes=20480;
inline constexpr unsigned DataCacheLine=dg_board::DataCacheLine;
static_assert(StagingAlignment%DataCacheLine==0,"LCD staging alignment must cover the target data cache line");
static_assert((StagingBytes/2)%StagingAlignment==0);
constexpr int cat_coordinate(int value){return value*PanelWidth/412;}
}
