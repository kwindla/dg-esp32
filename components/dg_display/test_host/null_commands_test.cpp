#include <gtest/gtest.h>
#include "dg_display/backend_commands.h"
#include "dg_board/board.h"
#include "../backends/null/commands.h"
#include <string>
#include <stdexcept>
namespace {std::string output;bool initialized=false;unsigned runtime_calls=0;}
namespace dg_board {
const BoardInfo& info(){static constexpr BoardInfo value{"host-null","host",{360,360,false},false,TouchKind::None,false,ConsoleKind::Uart};return value;}
}
namespace dg_display {
// The actual shared console dispatcher is linked. Its downstream runtime
// boundary represents the first state precondition, with no IDF dependencies.
void backend_runtime_command(const char*,const char*,bool,void (*out)(const char*)){
    ++runtime_calls;if(!initialized)throw std::runtime_error("disp init first");out("runtime called");
}
}
void collect(const char* text){output+=text;}
TEST(NullCommands,ActualDispatchRejectsBeforeRuntimeStateAndPrintsPlainBoardName){
    for(bool ready:{false,true}){
        initialized=ready;runtime_calls=0;
        for(auto* command:{"init","clock","r3-info","r3-stress","r3-stress-status","r4-guard","r4-crypto",
            "r5-hash","r5-hash-status","te","dma-trace","r2-fault","r2-reset-metrics","r2",
            "spi-trace","qualify","measure"}){
            output.clear();EXPECT_NO_THROW(dg_display::command(command,true,collect));
            EXPECT_EQ(output,std::string("\nunsupported board=host-null cmd=")+command+"\n");
        }
        EXPECT_EQ(runtime_calls,0u);
    }
}
TEST(NullCommands,SupportedCommandsRetainRuntimePreconditions){
    initialized=false;runtime_calls=0;
    EXPECT_THROW(dg_display::command("status",true,collect),std::runtime_error);EXPECT_EQ(runtime_calls,1u);
    initialized=true;output.clear();dg_display::command("status",true,collect);EXPECT_EQ(output,"runtime called");
}
TEST(NullCommands,LegacyHookErrorsUseTheSelectedBoardAndPlainOutput){
    output.clear();dg_display::null_unsupported("r4-guard",collect);
    EXPECT_EQ(output,"\nunsupported board=host-null cmd=r4-guard\n");
    testing::internal::CaptureStdout();dg_display::null_unsupported("clock");
    EXPECT_EQ(testing::internal::GetCapturedStdout(),"\nunsupported board=host-null cmd=clock\n");
}
