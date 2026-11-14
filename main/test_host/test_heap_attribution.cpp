#include "heap_attribution.h"
#include "esp_heap_trace.h"
#include "esp_heap_caps.h"
#include "heap_new_callers.h"
#include "lwip/tcpip.h"
#include "lwip/priv/tcp_priv.h"
#include <gtest/gtest.h>
#include <nlohmann/json.hpp>
#include <cstdlib>
#include <string>
#include <vector>
static std::vector<nlohmann::json> output;
static bool api_running=false,overflow=false;
static size_t count=4,allocations=0,get_calls=0;
static int fail_stop=0;
static void emit(const char* text){std::string line=text;auto at=line.find('{');ASSERT_NE(at,std::string::npos);ASSERT_LT(line.size(),640u);output.push_back(nlohmann::json::parse(line.substr(at)));}
void* heap_caps_calloc(size_t n,size_t s,unsigned caps){EXPECT_TRUE(n==4096u || n==dg::HeapNewCapacity);EXPECT_EQ(caps,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);++allocations;return std::calloc(n,s);}
void heap_caps_free(void* p){std::free(p);}
size_t heap_caps_get_free_size(unsigned caps){EXPECT_EQ(caps,MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);return 350000;}
size_t heap_caps_get_largest_free_block(unsigned){return 249856;}
bool esp_ptr_internal(const void* p){return reinterpret_cast<uintptr_t>(p)<0x50000000;}
bool esp_ptr_external_ram(const void* p){return !esp_ptr_internal(p);}
esp_err_t heap_trace_init_standalone(heap_trace_record_t* p,size_t n){EXPECT_NE(p,nullptr);EXPECT_EQ(n,4096u);return 0;}
esp_err_t heap_trace_start(heap_trace_mode_t mode){EXPECT_EQ(mode,HEAP_TRACE_LEAKS);EXPECT_FALSE(api_running);api_running=true;return 0;}
esp_err_t heap_trace_stop(){if(fail_stop)return fail_stop;EXPECT_TRUE(api_running);api_running=false;return 0;}
esp_err_t heap_trace_summary(heap_trace_summary_t* s){EXPECT_FALSE(api_running);*s={count,size_t(overflow),count,1000,990};return 0;}
esp_err_t heap_trace_get(size_t i,heap_trace_record_t* r){EXPECT_FALSE(api_running);++get_calls;*r={reinterpret_cast<void*>(0x4ff00000+i*1024),512,false,{reinterpret_cast<void*>(0x40000100)}};return 0;}
tcp_pcb *tcp_active_pcbs=nullptr,*tcp_tw_pcbs=nullptr,*tcp_bound_pcbs=nullptr;
err_t tcpip_api_call(err_t (*fn)(tcpip_api_call_data*),tcpip_api_call_data* data){return fn(data);}
class HeapAttribution:public ::testing::Test {
protected:
    void SetUp() override{fail_stop=0;overflow=false;count=4;get_calls=0;dg::heap_attribution_command("init",true,emit);output.clear();}
    void TearDown() override{fail_stop=0;if(api_running)dg::heap_attribution_command("stop",true,emit);}
    void command(const char* text){dg::heap_attribution_command(text,true,emit);}
};
TEST_F(HeapAttribution,NoAllocationOnRepeatInitAndNoTraceUntilExplicitStart){auto n=allocations;command("init");EXPECT_EQ(allocations,n);EXPECT_FALSE(api_running);}
TEST_F(HeapAttribution,AllCommandsRefuseActiveSessionIncludingExport){for(auto text:{"init","start","stop","page 0","tcp"})EXPECT_THROW(dg::heap_attribution_command(text,false,emit),std::runtime_error);EXPECT_TRUE(output.empty());}
TEST_F(HeapAttribution,StartStopAndBoundedPagesPreserveWindowAndStack){
    command("start");auto id=output.back()["id"];EXPECT_TRUE(api_running);EXPECT_EQ(get_calls,0u);
    dg::heap_new_remember(reinterpret_cast<void*>(0x4ff00000),0x48001002);
    EXPECT_THROW(command("page 0"),std::runtime_error);
    EXPECT_THROW(command("start"),std::runtime_error);
    command("stop");EXPECT_FALSE(api_running);EXPECT_EQ(output.back()["records"],4);EXPECT_FALSE(output.back()["overflow"]);
    output.clear();command("page 0");ASSERT_EQ(output.size(),4u);EXPECT_EQ(output.back()["next"],3);
    for(unsigned i=0;i<3;++i){EXPECT_EQ(output[i]["index"],i);EXPECT_EQ(output[i]["id"],id);EXPECT_EQ(output[i]["callers"].size(),8u);}
    EXPECT_EQ(output[0]["new_caller"],0x48001002);
    output.clear();command("page 1");ASSERT_EQ(output.size(),2u);EXPECT_EQ(output.back()["next"],4);EXPECT_EQ(get_calls,4u);
    EXPECT_THROW(command("page 2"),std::runtime_error);
}
TEST_F(HeapAttribution,OverflowIsExplicitAndEmptyWindowHasNoPage){overflow=true;count=0;command("start");command("stop");EXPECT_TRUE(output.back()["overflow"]);EXPECT_EQ(output.back()["records"],0);EXPECT_THROW(command("page 0"),std::runtime_error);}
TEST_F(HeapAttribution,ApiStopFailureCannotEnableExport){command("start");fail_stop=1;
    EXPECT_THROW(command("stop"),std::runtime_error);
    EXPECT_THROW(command("page 0"),std::runtime_error);
}
TEST_F(HeapAttribution,MalformedAndUnboundedCommandsAreRejected){for(auto text:{"","start junk","init 1","page","page -1","page 1junk","page 10000","page 0 x","unknown"})EXPECT_THROW(command(text),std::runtime_error);}
TEST_F(HeapAttribution,TcpStateCountsTimeWaitInCoreContext){
    tcp_pcb second{nullptr},first{&second};tcp_tw_pcbs=&first;
    command("tcp");EXPECT_EQ(output.back()["time_wait"],2);EXPECT_EQ(output.back()["time_wait_ms"],120000);
    EXPECT_EQ(output.back()["memp_num_tcp_pcb"],16);tcp_tw_pcbs=nullptr;
}
