#include "dg_board/console.h"
#include "esp_log.h"
namespace dg_board {
namespace {void log_audio_boot([[maybe_unused]] const char* hash){ESP_LOGI("dg-esp32","esp32p4 audio elf_sha256=%s",hash);}}
const BoardInfo& info(){
    static constexpr BoardInfo value{"p4xc","esp32p4",{800,800,true},false,TouchKind::Polled,true,console_kind,false,false,true,"simple UI unavailable",log_audio_boot};
    return value;
}
}
