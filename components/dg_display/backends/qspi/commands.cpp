#include "dg_display/backend_commands.h"
namespace dg_display {
void backend_boot(){}
bool backend_command_supported(const char*) noexcept{return true;}
}
