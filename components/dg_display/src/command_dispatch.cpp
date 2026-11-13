#include "dg_display/backend_commands.h"
#include "dg_board/board.h"
#include <cstdio>
#include <cstring>
#include <stdexcept>
namespace dg_display {
void backend_runtime_command(const char* action,const char* payload,bool idle,void (*out)(const char*));
void command(const char* args,bool idle,void (*out)(const char*)){
    char action[32]{};int end=0;
    if(std::sscanf(args," %31s%n",action,&end)!=1)throw std::runtime_error("disp action required");
    const char* payload=args+end;while(*payload==' ')++payload;
    // Reject hardware commands before the runtime can require initialized state.
    if(!backend_command_supported(action)){
        char text[128];std::snprintf(text,sizeof(text),"\nunsupported board=%s cmd=%s\n",dg_board::info().name,action);
        out(text);return;
    }
    backend_runtime_command(action,payload,idle,out);
}
}
