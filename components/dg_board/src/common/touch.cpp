#include "dg_board/touch.h"
#include "dg_board/board.h"
#include "dg_board/i2c.h"
#include "dg_board/backlight.h"
#include "dg_board/lcd.h"
#include "dg_board/canvas.h"
#include "sdkconfig.h"
#include "dg_board/board.h"
#include "../touch_backend.h"
#include <atomic>
namespace dg_board {
namespace {
TouchStatus touch_state;
DoubleTap gesture;
std::atomic<bool> injected_pending{false};
std::atomic<uint32_t> injected_count{0},injected_ignored{0};
std::atomic<bool> published_initialized{false};
std::atomic<uint32_t> published_reads{0},published_errors{0},published_coalesced{0};
std::atomic<uint32_t> published_taps{0},published_doubles{0},published_ignored{0},published_rejected{0};
void publish(){
    const auto c=gesture.counts();published_reads=touch_state.reads;published_errors=touch_state.read_errors;published_coalesced=touch_state.coalesced;
    published_taps=c.taps;published_doubles=c.double_taps;published_ignored=c.ignored;published_rejected=c.rejected;
}
uint32_t consumed=0;
bool accepting=false,polled_once=false;
uint32_t last_poll=0;
}
bool detail::touch_to_logical(uint16_t& x,uint16_t& y)noexcept{
    if(x<CanvasOffsetX || y<CanvasOffsetY || x>=CanvasOffsetX+CanvasWidth*CanvasScale ||
       y>=CanvasOffsetY+CanvasHeight*CanvasScale)return false;
    x=(x-CanvasOffsetX)/CanvasScale;y=(y-CanvasOffsetY)/CanvasScale;return true;
}
esp_err_t touch::init(){
    if(touch_state.initialized)return ESP_OK;
    auto result=detail::touch_hardware_init();
    if(result!=ESP_OK)return result;
    touch_state.initialized=true;published_initialized=true;return ESP_OK;
}
bool touch::poll(uint32_t now,bool accept){
    if(!touch_state.initialized)return false;
    const bool injected=injected_pending.exchange(false);
    const bool delivered=injected && accept;
    if(injected){++injected_count;if(!accept)++injected_ignored;gesture.cancel();}
    if(accept!=accepting){gesture.cancel();accepting=accept;}
    const bool polled=info().touch==TouchKind::Polled;
    if(polled){
        // First call reads immediately; no catch-up bursts after a delayed
        // owner. Unsigned elapsed time also covers the millisecond wrap.
        if(polled_once && now-last_poll<20)return delivered;
        polled_once=true;last_poll=now;
    }else{
        const auto count=detail::touch_interrupt_count();
        if(count==consumed)return delivered;
        // The controller pulses IRQ every report while a contact is held, so several
        // edges per 20 ms poll are normal. Counted only: no human lifts and touches
        // again within one poll (S3 demo, 2026-11-06: every physical tap was cancelled).
        touch_state.coalesced+=count-consumed-1;consumed=count;
    }
    struct Publish {~Publish(){publish();}} publish_on_exit;
    TouchPoint point{};
    const auto result=touch::read_raw(point);
    ++touch_state.reads;
    // No-ready is not a release. Reset/reprobe invalidates any old gesture.
    if(polled && result==ESP_ERR_NOT_FINISHED)return delivered;
    if(result!=ESP_OK){++touch_state.read_errors;gesture.cancel();return delivered;}
    // Espressif CST816S driver: 0x02 count, 0x03/04 X, 0x05/06 Y.
    auto x=point.x,y=point.y;
    if(point.contacts>1 || (point.contacts && (x>=info().panel.width || y>=info().panel.height))){gesture.cancel();return delivered;}
    if(polled && point.contacts){
        // Inverse of the board's logical canvas layout. Border contacts do
        // not map into the scene, and never become a synthetic release.
        if(!detail::touch_to_logical(x,y)){gesture.cancel();return delivered;}
    }
    return gesture.sample(now,point.contacts!=0,x,y,accept) || delivered;
}
bool touch::inject_double_tap()noexcept{if(!published_initialized.load())return false;return !injected_pending.exchange(true);}
TouchStatus touch::status(){
    TouchStatus snapshot;snapshot.initialized=published_initialized.load();
    snapshot.interrupts=detail::touch_interrupt_count();
    snapshot.injected=injected_count.load();snapshot.injected_ignored=injected_ignored.load();
    snapshot.reads=published_reads.load();snapshot.read_errors=published_errors.load();snapshot.coalesced=published_coalesced.load();
    snapshot.gestures={published_taps.load(),published_doubles.load(),published_ignored.load(),published_rejected.load()};return snapshot;
}
}
