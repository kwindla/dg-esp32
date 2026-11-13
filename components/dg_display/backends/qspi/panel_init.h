#pragma once
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
namespace dg_display {
struct PanelCensus {
    unsigned (*before)()=nullptr;
    void (*after)(const char*,unsigned,esp_err_t)=nullptr;
};
esp_err_t qspi_init_panel(esp_lcd_panel_io_handle_t,esp_lcd_panel_handle_t*,PanelCensus={});
// Keep per-object census boundaries while the actual command path can also be
// executed against the host's recording panel IO (real vendor driver sources).
template<class F> esp_err_t panel_step(PanelCensus observer,const char* name,F action){
    const auto before=observer.before?observer.before():0;
    const auto error=action();if(observer.after)observer.after(name,before,error);return error;
}
}
