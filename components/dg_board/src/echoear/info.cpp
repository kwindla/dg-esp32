#include "dg_board/console.h"
#include "esp_log.h"
namespace dg_board {
namespace {void log_audio_boot([[maybe_unused]] const char* hash){ESP_LOGI("dg-esp32","ESP32-S3 step 7 audio elf_sha256=%s",hash);}}
const BoardInfo& info(){
    static constexpr BoardInfo value{"echoear","esp32s3",{360,360,true},true,TouchKind::Irq,true,console_kind,true,true,true,"demo needs EchoEar panel",log_audio_boot};
    return value;
}
}
