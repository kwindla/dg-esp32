#include "../backends/qspi/panel_init.h"
#include "dg_board/backlight.h"
#include "driver/gpio.h"
#include "freertos/task.h"
#include <cstdio>
#include <cstdlib>
namespace {
unsigned transfers=0,fail_at=0;
unsigned before(){return 0;}
void after(const char* name,unsigned,esp_err_t error){std::printf("STEP %s %d\n",name,error);}
}
extern "C" esp_err_t esp_lcd_panel_io_tx_param(esp_lcd_panel_io_handle_t,int command,const void* bytes,size_t size){
    std::printf("CMD %08x",unsigned(command));
    for(size_t i=0;i<size;++i)std::printf(" %02x",static_cast<const unsigned char*>(bytes)[i]);
    if(++transfers==fail_at){std::printf(" FAIL\n");return ESP_FAIL;}
    std::printf("\n");return ESP_OK;
}
extern "C" esp_err_t esp_lcd_panel_io_tx_color(esp_lcd_panel_io_handle_t,int,const void*,size_t){std::abort();}
extern "C" void vTaskDelay(TickType_t ticks){std::printf("DELAY %u\n",unsigned(ticks));}
extern "C" esp_err_t gpio_config(const gpio_config_t*){std::abort();}
extern "C" esp_err_t gpio_reset_pin(int){std::abort();}
extern "C" esp_err_t gpio_set_level(int,int){std::abort();}
namespace dg_board {esp_err_t backlight::init(){std::printf("BACKLIGHT\n");return ESP_OK;}}
int main(int argc,char** argv){
    std::setvbuf(stdout,nullptr,_IONBF,0);
    if(argc>1)fail_at=unsigned(std::strtoul(argv[1],nullptr,10));
    esp_lcd_panel_handle_t panel=nullptr;
    auto error=dg_display::qspi_init_panel(reinterpret_cast<esp_lcd_panel_io_handle_t>(1),&panel,{before,after});
    std::printf("RESULT %d\n",error);
    if(panel)esp_lcd_panel_del(panel);
    return error==ESP_OK?0:2;
}
