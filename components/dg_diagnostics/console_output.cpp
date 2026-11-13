#include "dg_console.h"
#include "dg_console_delivery.h"
#include "dg_console_ack.h"
#include "console_backend.h"
#include "dg_board/board.h"
#include "dg_dump_pacing.h"
#include "sdkconfig.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/ringbuf.h"
#include "freertos/task.h"
#include <atomic>
#include <cstdio>
#include <new>
#include <stdexcept>
namespace dg_console {
namespace {
RingbufHandle_t queue=nullptr;
AckMailbox acknowledgements;
unsigned bulk_posted=0,bulk_retired=0; // UART admission lock / sole drain owner, respectively.
StaticRingbuffer_t control;
constexpr size_t format_bytes=1536;
char* format_buffer=nullptr;
std::atomic<unsigned> queued{0},disconnected{0},full{0},transport_drops{0};
void drain(void*) {
    bool boundary=false;
    for(;;) {
        const bool uart=dg_board::info().console==dg_board::ConsoleKind::Uart;
        auto send=[](const char* p,size_t n){return backend().send(p,n);};
        auto clock=[] {return esp_timer_get_time();};
        auto yield=[] {backend().wait();};
        if(uart && acknowledgements.ready(bulk_retired)) {
            // All earlier admitted response records have retired. Unlike
            // best-effort logs, ACK progress is never discarded on a deadline.
            if(boundary){if(!deliver("\n",1,send,clock,yield))continue;boundary=false;}
            if(!acknowledgements.advance(send))yield();
            continue;
        }
        size_t size=0;
        auto* item=static_cast<char*>(xRingbufferReceive(queue,&size,uart?std::max(TickType_t(1),pdMS_TO_TICKS(1)):portMAX_DELAY));
        if(!item)continue;
        bool ok=true;
        // A dropped partial line must not splice into the next evidence record.
        if(boundary)ok=deliver("\n",1,send,clock,yield);
        if(ok)ok=deliver(item,size,send,clock,yield);
        boundary=!ok;
        if(!ok)++transport_drops;
        vRingbufferReturnItem(queue,item);
        if(uart)++bulk_retired;
    }
}
}
bool connected(){return backend().connected();}
void pace_dump(size_t bytes,unsigned usb_ms) {
    unsigned baud=1;
#if CONFIG_ESP_CONSOLE_UART
    baud=CONFIG_ESP_CONSOLE_UART_BAUDRATE;
#endif
    const auto us=dump_delay_us(dg_board::info().console==dg_board::ConsoleKind::Uart,baud,bytes,usb_ms);
    if(!us)return; // USB framing lines preserve their original no-delay path.
    // Round up to ticks: never run faster than the measured wire budget.
    vTaskDelay(std::max(TickType_t(1),TickType_t((us+portTICK_PERIOD_MS*1000-1)/(portTICK_PERIOD_MS*1000))));
}
bool ready(){return queue!=nullptr;}
bool write(const char* text,size_t bytes) {
    if(!bytes)return true;
    if(!connected())++disconnected; // observation, not a delivery veto
    const bool uart=dg_board::info().console==dg_board::ConsoleKind::Uart;
    // Serialize UART admission order with the ACK fence. Recursive because log
    // formatting/console_line already holds stdout; no send or wait here.
    if(uart)flockfile(stdout);
    const auto admitted=enqueue_record(text,bytes,
        [](const char* p,size_t n){return queue && xRingbufferSend(queue,p,n,0)==pdTRUE;});
    if(admitted==Admission::Full){++full;if(uart)funlockfile(stdout);return false;}
    if(uart)++bulk_posted;
    ++queued;if(uart)funlockfile(stdout);return true;
}
int vprint(const char* format,va_list args) {
    // Shared scratch avoids adding 1536 bytes to sys_evt and driver stacks.
    // This recursive stdio lock is also held around IDF log-v2 fragments.
    // All runtime holders only format/enqueue: none drains USB under the lock.
    flockfile(stdout);
    const int n=std::vsnprintf(format_buffer,format_bytes,format,args);
    if(n<0 || size_t(n)>=format_bytes)++full;
    else write(format_buffer,size_t(n));
    funlockfile(stdout);return n;
}
bool ack_pending(){return dg_board::info().console==dg_board::ConsoleKind::Uart && acknowledgements.pending();}
void acknowledge(const char* text,size_t bytes){
    if(dg_board::info().console!=dg_board::ConsoleKind::Uart){write(text,bytes);return;}
    // Serial command admission guarantees at most two ACKs per command. A
    // violation is a programming error, never a silent best-effort queue drop.
    flockfile(stdout);
    const bool admitted=acknowledgements.post(text,bytes,bulk_posted);
    funlockfile(stdout);
    if(!admitted)throw std::runtime_error("console ACK admission invariant");
    ++queued;
}
Stats stats(){return {queued.load(),disconnected.load(),full.load(),transport_drops.load(),backend().rx_stats(),acknowledgements.pending()};}
void init() {
    backend().init();
    constexpr size_t bytes=65536;
    auto* storage=static_cast<uint8_t*>(heap_caps_malloc(bytes,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    format_buffer=static_cast<char*>(heap_caps_malloc(format_bytes,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT));
    if(!storage || !format_buffer)throw std::bad_alloc();
    queue=xRingbufferCreateStatic(bytes,RINGBUF_TYPE_NOSPLIT,storage,&control);
    if(!queue)throw std::bad_alloc();
    esp_log_set_vprintf(vprint);
    if(xTaskCreatePinnedToCoreWithCaps(drain,"console-output",4096,nullptr,1,nullptr,0,
        MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT)!=pdPASS)throw std::bad_alloc();
}
}
