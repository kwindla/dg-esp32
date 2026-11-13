#include "console_backend.h"
#include "dg_board/board.h"
#include "dg_dump_check.h"
#include "dg_dump_pacing.h"
#include "dg_heap_region.h"
#include "driver/uart.h"
#include "driver/usb_serial_jtag.h"
#include "esp_timer.h"
#include "freertos/task.h"
#include "console_support.h"
#include <gtest/gtest.h>
#include <string>
namespace {
unsigned stage=0;
bool attached=false;
std::string sent;
void (*timer_callback)(void*);
bool notified=false;
bool notify_on_start=true;
int timer_status=ESP_OK;
unsigned sleeps=0,waits=0,clears=0,event_at=0;
void (*event_task)(void*);
struct EventsDone {};
}
int esp_timer_create(const esp_timer_create_args_t* args,esp_timer_handle_t* handle) {
    EXPECT_EQ(stage,4u);timer_callback=args->callback;*handle=reinterpret_cast<void*>(1);return 0;
}
int esp_timer_start_once(esp_timer_handle_t handle,uint64_t delay) {
    EXPECT_EQ(handle,reinterpret_cast<void*>(1));EXPECT_EQ(delay,250u);
    if(notify_on_start)timer_callback(nullptr);
    return timer_status;
}
void vTaskDelay(unsigned ticks) {EXPECT_EQ(ticks,1u);++sleeps;}
TaskHandle_t xTaskGetCurrentTaskHandle() {return reinterpret_cast<void*>(2);}
void xTaskNotifyGive(TaskHandle_t handle) {EXPECT_EQ(handle,reinterpret_cast<void*>(2));notified=true;}
unsigned ulTaskNotifyTake(unsigned clear,unsigned timeout) {
    EXPECT_EQ(clear,pdTRUE);
    if(!timeout)++clears;
    else {EXPECT_EQ(timeout,3u);++waits;}
    const unsigned result=notified;notified=false;return result;
}
int xTaskCreate(void(*task)(void*),const char* name,unsigned stack,void*,unsigned priority,TaskHandle_t*) {
    EXPECT_STREQ(name,"console-uart-rx");EXPECT_EQ(stack,3072u);EXPECT_EQ(priority,5u);event_task=task;return pdPASS;
}
int xQueueReceive(QueueHandle_t queue,void* output,unsigned wait) {
    EXPECT_EQ(queue,reinterpret_cast<void*>(3));EXPECT_EQ(wait,portMAX_DELAY);
    constexpr int events[]={UART_DATA,UART_FIFO_OVF,UART_BUFFER_FULL,UART_FRAME_ERR,UART_PARITY_ERR,UART_FIFO_OVF};
    if(event_at==std::size(events))throw EventsDone{};
    static_cast<uart_event_t*>(output)->type=events[event_at++];return pdTRUE;
}
namespace dg_board {
const BoardInfo& info() {
    static const BoardInfo board{"test",TEST_P4?"esp32p4":"esp32s3",{},false,TouchKind::None,false,
        TEST_P4?ConsoleKind::Uart:ConsoleKind::UsbSerialJtag};
    return board;
}
}
int uart_param_config(int port,const uart_config_t* cfg) {
    EXPECT_EQ(stage++,0u);EXPECT_EQ(port,0);EXPECT_EQ(cfg->baud_rate,2000000);
    EXPECT_EQ(cfg->data_bits,8);EXPECT_EQ(cfg->flow_ctrl,0);return 0;
}
int uart_set_pin(int port,int tx,int rx,int rts,int cts) {
    EXPECT_EQ(stage++,1u);EXPECT_EQ(port,0);EXPECT_EQ(tx,37);EXPECT_EQ(rx,38);
    EXPECT_EQ(rts,-1);EXPECT_EQ(cts,-1);return 0;
}
int uart_driver_install(int port,int rx,int tx,int events,QueueHandle_t* queue,int flags) {
    EXPECT_EQ(stage++,2u);EXPECT_EQ(port,0);EXPECT_EQ(rx,8192);EXPECT_EQ(tx,0);
    EXPECT_EQ(events,32);EXPECT_NE(queue,nullptr);*queue=reinterpret_cast<void*>(3);EXPECT_EQ(flags,0);return 0;
}
void uart_vfs_dev_use_driver(int port) {EXPECT_EQ(stage++,3u);EXPECT_EQ(port,0);}
int uart_tx_chars(int port,const char* text,uint32_t size) {
    EXPECT_EQ(stage,4u);EXPECT_EQ(port,0);
    // Simulate a FIFO accepting only three bytes, without waiting.
    const auto n=std::min(size,3u);sent.append(text,n);return n;
}
int usb_serial_jtag_driver_install(const usb_serial_jtag_driver_config_t* cfg) {
    EXPECT_EQ(stage++,0u);EXPECT_EQ(cfg->tx_buffer_size,4096u);EXPECT_EQ(cfg->rx_buffer_size,1024u);return 0;
}
void usb_serial_jtag_vfs_use_driver() {EXPECT_EQ(stage++,1u);}
bool usb_serial_jtag_is_connected() {return attached;}
int usb_serial_jtag_write_bytes(const char* text,size_t size,unsigned wait) {
    EXPECT_EQ(wait,0u);sent.append(text,size);return size;
}
TEST(Backend, BoardSelectsDriverAndPreservesNonblockingAttempts) {
    stage=0;sent.clear();
    const auto& backend=dg_console::backend();backend.init();
    if(TEST_P4) {
        EXPECT_EQ(&backend,&dg_console::uart_backend);EXPECT_EQ(stage,4u);
        EXPECT_TRUE(backend.connected());EXPECT_EQ(backend.send("hello",5),3);EXPECT_EQ(sent,"hel");
    }else {
        EXPECT_EQ(stage,2u);attached=false;EXPECT_FALSE(backend.connected());
        // An absent USB observation is not a send veto.
        EXPECT_EQ(backend.send("OK #1\n",6),6);EXPECT_EQ(sent,"OK #1\n");
        attached=true;EXPECT_TRUE(backend.connected());
    }
    backend.wait();
}
TEST(DumpCheck, ExactLfJoinedTextAndEmptyDigest) {
    char output[128];
    dg_console::DumpCheck empty;empty.finish("DUMP",output,sizeof output);
    EXPECT_STREQ(output,"\nDUMP CHECK lines=0 sha256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\n");
    dg_console::DumpCheck hash;hash.record("\nabc\n");hash.finish("DUMP",output,sizeof output);
    EXPECT_STREQ(output,"\nDUMP CHECK lines=1 sha256=ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad\n");
    dg_console::DumpCheck small;EXPECT_THROW(small.finish("DUMP",output,4),std::runtime_error);
}
TEST(DumpCheck, EveryUpdateUsesOneInternalScratchIncludingLfAndChunks) {
    const auto allocations=hash_fake::allocations;
    const auto live=hash_fake::internal.size();
    std::string caller(2500,'x');hash_fake::callers={{caller.data(),caller.size()}};
    hash_fake::updates.clear();hash_fake::operations.clear();
    {
        dg_console::DumpCheck check;EXPECT_EQ(hash_fake::allocations,allocations+1);
        check.record(caller.c_str());check.record(caller.c_str());
        ASSERT_EQ(hash_fake::updates.size(),7u); // 3 chunks, LF, 3 chunks.
        EXPECT_EQ(hash_fake::updates[3].bytes,1u);
        for(const auto& update:hash_fake::updates) {
            EXPECT_TRUE(hash_fake::is_internal(update.pointer,update.bytes));
            EXPECT_NE(update.pointer,caller.data());EXPECT_LE(update.bytes,1024u);
            EXPECT_EQ(update.pointer,hash_fake::updates.front().pointer);
        }
        EXPECT_EQ(hash_fake::allocations,allocations+1);
        char output[128];check.finish("DUMP",output,sizeof output);
        const auto& block=hash_fake::internal.back();
        ASSERT_FALSE(hash_fake::operations.empty());
        for(const auto& operation:hash_fake::operations) {
            EXPECT_TRUE(hash_fake::inside(block,operation.pointer,operation.bytes));
            EXPECT_EQ(operation.pointer,hash_fake::operations.front().pointer);
        }
        for(const auto& update:hash_fake::updates)
            EXPECT_TRUE(hash_fake::inside(block,update.pointer,update.bytes));
        EXPECT_NE(hash_fake::operations.front().pointer,hash_fake::updates.front().pointer);
    }
    hash_fake::callers.clear();EXPECT_EQ(hash_fake::internal.size(),live);
}
TEST(Backend, RxErrorsAndTimerFailuresAreObservableAndBounded) {
    if(!TEST_P4)return;
    stage=0;dg_console::backend().init();event_at=0;
    const auto before=dg_console::backend().rx_stats();
    EXPECT_THROW(event_task(nullptr),EventsDone);
    const auto after=dg_console::backend().rx_stats();
    EXPECT_EQ(after.fifo_overflows-before.fifo_overflows,2u);
    EXPECT_EQ(after.buffer_full-before.buffer_full,1u);
    EXPECT_EQ(after.frame_errors-before.frame_errors,1u);
    EXPECT_EQ(after.parity_errors-before.parity_errors,1u);
    sleeps=waits=clears=0;
    notified=true;timer_status=ESP_ERR_INVALID_STATE;notify_on_start=false;
    EXPECT_NO_THROW(dg_console::backend().wait());
    EXPECT_EQ(clears,1u);EXPECT_EQ(waits,1u);EXPECT_FALSE(notified);
    timer_status=ESP_ERR_NO_MEM;
    EXPECT_NO_THROW(dg_console::backend().wait());
    EXPECT_EQ(sleeps,1u);EXPECT_EQ(waits,1u);
    timer_status=ESP_OK;notify_on_start=true;
}
TEST(ConsoleSupport, CodecCapabilityBuildReasonsAndLegacyInformationalCommands) {
    using dg::ConsoleSupport;using dg::console_support;
    EXPECT_EQ(console_support("mic-analog-gain",true,false,false,true),ConsoleSupport::Available);
    EXPECT_EQ(console_support("mic-analog-gain",false,true,true,true),ConsoleSupport::Board);
    EXPECT_EQ(console_support("aec-record",true,false,false,true),ConsoleSupport::AecDisabled);
    EXPECT_EQ(console_support("aec-clock",true,false,false,true),ConsoleSupport::Available);
    EXPECT_EQ(console_support("cache-counters",true,false,false,true),ConsoleSupport::CacheDisabled);
    EXPECT_EQ(console_support("cache-counters",true,false,true,false),ConsoleSupport::Board);
    for(const char* command:{"power","touch-status","touch-enable","touch-inject"})
        EXPECT_EQ(console_support(command,false,false,false,true),ConsoleSupport::Available);
}
TEST(Pacing, UsbIsUnchangedAndUartScalesWithWireBytes) {
    EXPECT_EQ(dg_console::dump_delay_us(false,1,530),8000u);
    EXPECT_EQ(dg_console::dump_delay_us(false,1,530,4),4000u);
    EXPECT_EQ(dg_console::dump_delay_us(true,2000000,528),3300u);
    EXPECT_EQ(dg_console::dump_delay_us(true,1000000,528),6600u);
    EXPECT_GE(dg_console::dump_delay_us(true,921600,1)*921600,12500000u);
}
TEST(HeapRegions, UsesTargetBoundariesAndSeparatesReserveAndExternal) {
    using dg_console::heap_region;
    EXPECT_EQ(heap_region(SOC_RTC_DRAM_LOW,true,false),4u);
    EXPECT_EQ(heap_region(SOC_RTC_DATA_LOW,true,false),4u);
#if TEST_P4_REV3
    constexpr auto stack_start=SOC_ROM_STACK_START_REV2;
#else
    constexpr auto stack_start=SOC_ROM_STACK_START;
#endif
    EXPECT_EQ(heap_region(stack_start-SOC_ROM_STACK_SIZE,true,false),1u);
    EXPECT_EQ(heap_region(stack_start-SOC_ROM_STACK_SIZE,true,true),3u);
    EXPECT_EQ(heap_region(0,true,true),3u);EXPECT_EQ(heap_region(0,false,false),5u);
#if TEST_P4
    EXPECT_EQ(heap_region(0x4ff80000,true,false),TEST_P4_REV3?0u:1u);
    EXPECT_EQ(heap_region(0x4ff3afbf,true,false),0u);
    EXPECT_EQ(heap_region(0x4ff40000,true,false),TEST_P4_REV3?0u:1u);
    EXPECT_EQ(heap_region(0x4ffbafc0,true,false),TEST_P4_REV3?1u:0u);
    EXPECT_EQ(heap_region(0x4ffc0000,true,false),0u);
    EXPECT_EQ(heap_region(0x3fcf0000,false,false),5u);
#else
    EXPECT_EQ(heap_region(0x3fcb0000,true,false),0u);
    EXPECT_EQ(heap_region(0x3fcf0000,true,false),2u);
#endif
}
