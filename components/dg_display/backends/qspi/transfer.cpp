#include "../../src/runtime_state.h"
#include "pins.h" // Selected backend include directory supplies native pins.
#include "dg_board/backlight.h"
namespace dg_display {namespace detail {
void te_interrupt(void*){uint32_t n=state->te_count.load(std::memory_order_relaxed);state->te_stamps[n%128]=uint32_t(esp_timer_get_time());state->te_count.store(n+1,std::memory_order_release);}
void init_te(){
    auto before=heap_caps_get_free_size(Internal);gpio_config_t config{};config.pin_bit_mask=1ull<<LcdTe;config.mode=GPIO_MODE_INPUT;config.intr_type=GPIO_INTR_POSEDGE;ESP_ERROR_CHECK(gpio_config(&config));ESP_ERROR_CHECK(dg_board::init_gpio_isr_service());ESP_ERROR_CHECK(gpio_isr_handler_add(static_cast<gpio_num_t>(LcdTe),te_interrupt,nullptr));census("te-non-iram-observer",before);
}
int te_gpio() noexcept{return LcdTe;}
bool await_dma(){
    if(!ulTaskNotifyTake(pdTRUE,pdMS_TO_TICKS(2000))){
        if(state->color_inflight.load()){dead_display(2);return false;}
        ++state->scheduler_delays; // Completion exists; notification wait was delayed.
    }
    return !state->dead.load() && !dma_bounces();
}
bool drain_dma(){
    if(state->dead)return false; // Never drain/delete/reuse on an absent completion.
    state->io_started=esp_timer_get_time();
    auto error=esp_lcd_panel_io_tx_param(state->io,-1,nullptr,0);
    state->io_started=0;
    if(error!=ESP_OK){dead_display(4);return false;}
    return !dma_bounces();
}
bool send(Rect r,uint8_t* bytes){
    if(state->dead || flash_mmap_remain() || (uintptr_t(bytes)%StagingAlignment) || (size_t(r.x1-r.x0)*size_t(r.y1-r.y0)*2)%16)return false;
    // One stripe must fit exactly one color transaction. Queue depth is one.
    configASSERT(size_t(r.x1-r.x0)*size_t(r.y1-r.y0)*2<=state->staging_bytes);
    if(!drain_dma())return false; // Recycle the previous stripe before replacing its timestamps.
    ++state->stripes_attempted;state->bytes_attempted.fetch_add(uint64_t(r.x1-r.x0)*unsigned(r.y1-r.y0)*2);
    ++state->stripe_context.stripe;state->stripe_context.x0=r.x0;state->stripe_context.y0=r.y0;state->stripe_context.x1=r.x1;state->stripe_context.y1=r.y1;state->stripe_context.bytes=unsigned((r.x1-r.x0)*(r.y1-r.y0)*2);
    auto requested=esp_timer_get_time();state->display_waiting=true;
    // Writer preference only while it is acquiring. No engine mutex is held.
    // The writer must still acquire the semaphore, never assume FIFO ownership.
    while(state->writer_waiting && esp_timer_get_time()-requested<2000000)vTaskDelay(1);
    auto remaining=2000000-(esp_timer_get_time()-requested);
    const bool acquired=remaining>0 && xSemaphoreTake(state->gate,pdMS_TO_TICKS((remaining+999)/1000))==pdTRUE;
    state->display_waiting=false;
    if(!acquired){++state->display_gate_timeouts;++state->scheduler_delays;return false;}
    state->display_token=true;
    if(state->dead){if(state->display_token.exchange(false))xSemaphoreGive(state->gate);return false;}
    configASSERT(flash_mmap_remain()==0);
    state->frame_gate_wait+=esp_timer_get_time()-requested;
    if(state->writer_waiting){++state->stripes_while_writer_waits;state->max_stripes_while_writer_waits=std::max(state->max_stripes_while_writer_waits.load(),state->stripes_while_writer_waits);}else state->stripes_while_writer_waits=0;
    auto started=esp_timer_get_time();auto prior=state->last_color_done.load();if(prior)state->scheduling_gap_max_us=std::max(state->scheduling_gap_max_us.load(),uint64_t(started-prior));
    state->stripe_spi_mask=0;state->stripe_start=started;state->stripe_completion=0;state->color_inflight=true;

    state->io_started=esp_timer_get_time();
    unsigned fault=1;
    const bool prequeue=state->r2_fault.compare_exchange_strong(fault,0);
    auto error=prequeue?ESP_FAIL:esp_lcd_panel_draw_bitmap(state->panel,r.x0,r.y0,r.x1,r.y1,bytes);
    if(prequeue)++state->send_failures;
    state->io_started=0;
    if(error!=ESP_OK){state->color_inflight=false;if(state->display_token.exchange(false))configASSERT(xSemaphoreGive(state->gate)==pdTRUE);return false;}
    ++state->stripes_queued;
    return !state->dead;
}
}}
#include "dg_display.h"
#include "../qspi/hardware.h"
#include "../qspi/panel.h"
namespace dg_display {
PanelBackend& panel_backend() noexcept{static detail::QspiPanel backend;return backend;}
}
