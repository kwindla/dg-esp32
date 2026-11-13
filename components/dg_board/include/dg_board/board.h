#pragma once
namespace dg_board {
enum class TouchKind {None,Irq,Polled};
enum class ConsoleKind {UsbSerialJtag,Uart};
struct PanelInfo {unsigned width,height;bool round;};
struct BoardInfo {
    const char* name;
    const char* chip;
    PanelInfo panel;
    bool has_battery;
    TouchKind touch;
    bool aec_reference;
    ConsoleKind console;
    bool internal_8bit_dma_alias=false,cache_counters=false,simple_ui=false;
    const char* simple_ui_error=nullptr;
    void (*log_audio_boot)(const char*)=nullptr;
};
const BoardInfo& info();
// First app_main action: settle the rail and establish backlight off.
void early_board_init();
}
