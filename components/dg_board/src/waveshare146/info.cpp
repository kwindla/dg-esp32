#include "dg_board/console.h"
#include "esp_log.h"
namespace dg_board {
namespace {void log_audio_boot([[maybe_unused]] const char* hash){ESP_LOGI("dg-esp32","ESP32-S3 step 7 audio elf_sha256=%s",hash);}}
const BoardInfo& info(){
    static constexpr BoardInfo value{"waveshare_146","esp32s3",{412,412,true},false,TouchKind::None,false,console_kind,true,true,false,"demo needs EchoEar panel",log_audio_boot};
    return value;
}
}
