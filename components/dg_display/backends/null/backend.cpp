#include "dg_display/null_panel.h"
#include "dg_board/canvas.h"
namespace dg_display {
PanelBackend& panel_backend() noexcept{
    static NullPanel panel({dg_board::CanvasWidth,dg_board::CanvasHeight,false,PixelFormat::Rgb565BE,size_t(dg_board::CanvasWidth),false});
    return panel;
}
}
