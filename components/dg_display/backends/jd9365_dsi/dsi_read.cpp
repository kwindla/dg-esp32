// Product replacement for IDF 6.1's unbounded DCS read polling.
// Transaction sequence matches esp_hal_lcd/mipi_dsi_hal.c, but every FIFO/
// read-busy wait blocks one tick and shares a finite transaction deadline.
#include "bounded_wait.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "hal/mipi_dsi_hal.h"
#include "hal/mipi_dsi_host_ll.h"
#include <cstdlib>

extern "C" void __wrap_mipi_dsi_hal_host_gen_read_dcs_command(mipi_dsi_hal_context_t* hal,uint8_t vc,
    uint32_t command,uint32_t command_bytes,void* ret_param,uint16_t param_size) {
    const int64_t deadline=esp_timer_get_time()+500000;
    auto clock=[] {return esp_timer_get_time();};
    auto pause=[] {vTaskDelay(1);};
    auto fail=[&](const char* phase,esp_err_t error=ESP_ERR_TIMEOUT) {
        ESP_LOGE("jd9365", "DCS read phase=%s command=%lu rc=%d",phase,(unsigned long)command,error);
        // HAL has a void API: fail closed rather than invent a panel identity
        // or continue with partial data. Coordinator failure/abort restores factory.
        ESP_ERROR_CHECK(error);std::abort();
    };
    if(!hal || !ret_param || !param_size || command_bytes!=1)fail("arguments",ESP_ERR_INVALID_ARG);
    auto wait=[&](auto ready,const char* phase) {
        if(!panel_probe::wait_ready(ready,clock,pause,deadline))fail(phase);
    };
    auto packet=[&](mipi_dsi_data_type_t type,uint16_t data) {
        wait([&] {return !mipi_dsi_host_ll_gen_is_cmd_fifo_full(hal->host);},"command_fifo");
        mipi_dsi_host_ll_gen_set_packet_header(hal->host,vc,type,uint8_t(data>>8),uint8_t(data));
    };
    packet(MIPI_DSI_DT_SET_MAXIMUM_RETURN_PKT,param_size);
    mipi_dsi_host_ll_enable_video_mode(hal->host,false);
    mipi_dsi_host_ll_enable_bta(hal->host,true);
    mipi_dsi_host_ll_gen_set_rx_vcid(hal->host,vc);
    packet(MIPI_DSI_DT_DCS_READ_0,uint16_t(command&0xff));
    wait([&] {return !mipi_dsi_host_ll_gen_is_read_cmd_busy(hal->host);},"read_busy");
    wait([&] {return !mipi_dsi_host_ll_gen_is_read_fifo_empty(hal->host);},"read_fifo");
    auto* output=static_cast<uint8_t*>(ret_param);unsigned count=0;
    while(!mipi_dsi_host_ll_gen_is_read_fifo_empty(hal->host)) {
        if(clock()>=deadline)fail("drain_fifo");
        auto word=mipi_dsi_host_ll_gen_read_payload_fifo(hal->host);
        for(unsigned i=0;i<4 && count<param_size;++i)output[count++]=uint8_t(word>>(8*i));
        pause();
    }
    if(count!=param_size)fail("short_response",ESP_ERR_INVALID_RESPONSE);
}
