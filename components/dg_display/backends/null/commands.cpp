#include "dg_display/backend_commands.h"
#include <cstring>
#include <cstdio>
#include "dg_board/board.h"
#include "commands.h"
namespace dg_display {
void null_unsupported(const char* action,void (*out)(const char*)){
    char text[128];std::snprintf(text,sizeof(text),"\nunsupported board=%s cmd=%s\n",dg_board::info().name,action);
    if(out)out(text);else std::printf("%s",text);
}
bool backend_command_supported(const char* action) noexcept{
    const char* unsupported[]={"init","clock","r3-info","r3-stress","r3-stress-status",
        "r4-guard","r4-crypto","r5-hash","r5-hash-status","te","dma-trace",
        "r2-fault","r2-reset-metrics","r2","spi-trace","qualify","measure"};
    for(auto* command:unsupported)if(!std::strcmp(action,command))return false;
    return true;
}
}
