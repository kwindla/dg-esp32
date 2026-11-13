#include "dg_console.h"
#include "dg_dump_output.h"
#include "dg_dump_check.h"
#include "console_backend.h"
#include "dg_board/board.h"
#include "freertos/ringbuf.h"
#include "freertos/task.h"
#include <gtest/gtest.h>
#include <algorithm>
#include <deque>
#include <string>
#include <fstream>
#include <cstdlib>
struct FakeRing {std::deque<std::string> rows;std::string active;size_t used=0,capacity=1024;};
namespace {
FakeRing ring;int64_t now=0;std::string wire;void(*drainer)(void*);
bool blocked=false;size_t block_after=SIZE_MAX;unsigned fifo=64,pauses=0;
struct Empty{};struct Stalled{};
size_t occupied(size_t n){return 8+(n+3)/4*4;}
void pump(){try{drainer(nullptr);}catch(const Empty&) {}}
void init_backend(){}
bool is_connected(){return true;}
int send(const char* p,size_t n){
    if(blocked || wire.size()>=block_after)return 0;
    n=std::min({n,size_t(fifo),block_after-wire.size()});wire.append(p,n);fifo-=n;return int(n);
}
void wait(){now+=250;fifo=64;if((blocked || wire.size()>=block_after) && now>1500000)throw Stalled{};}
class ConsoleOutput:public testing::Test {
    void SetUp()override{ring={};now=0;wire.clear();blocked=false;block_after=SIZE_MAX;fifo=64;pauses=0;dg_console::init();}
};
void output(const char* text){if(*text=='\n')++text;dg_console::write(text,std::strlen(text));}
}
namespace dg_board {
const BoardInfo& info(){static constexpr BoardInfo b{"test","test",{},false,TouchKind::None,false,
    TEST_P4?ConsoleKind::Uart:ConsoleKind::UsbSerialJtag};return b;}
}
namespace dg_console {
const Backend& backend(){static const Backend b{init_backend,is_connected,send,wait,[](){return RxStats{};}};return b;}
}
int64_t esp_timer_get_time(){return now;}
void vTaskDelay(unsigned ticks){now+=ticks*1000;++pauses;fifo=64;pump();}
int xTaskCreatePinnedToCoreWithCaps(void(*f)(void*),const char*,unsigned,void*,unsigned,TaskHandle_t*,int,unsigned){drainer=f;return pdPASS;}
RingbufHandle_t xRingbufferCreateStatic(size_t,unsigned,uint8_t*,StaticRingbuffer_t*){return &ring;}
int xRingbufferSend(RingbufHandle_t,const void* p,size_t n,unsigned timeout){
    EXPECT_EQ(timeout,0u);if(ring.used+occupied(n)>ring.capacity)return 0;
    ring.rows.emplace_back(static_cast<const char*>(p),n);ring.used+=occupied(n);return pdTRUE;
}
void* xRingbufferReceive(RingbufHandle_t,size_t* n,unsigned timeout){
    EXPECT_EQ(timeout,TEST_P4?1u:portMAX_DELAY);
    if(ring.rows.empty())throw Empty{};
    ring.active=ring.rows.front();ring.rows.pop_front();*n=ring.active.size();return ring.active.data();
}
void vRingbufferReturnItem(RingbufHandle_t,void*){ring.used-=occupied(ring.active.size());ring.active.clear();}
TEST_F(ConsoleOutput,SmallRingReproducesOldAckLossButReservedAckSurvives){
    const std::string row(500,'x');while(dg_console::write(row.data(),row.size())){}
    // This is the pre-fix ACK path, on the real production write() function.
    EXPECT_FALSE(dg_console::write("OK #11 netprobe\n",16));
    if(TEST_P4){
        const auto before=dg_console::stats();const std::string ack="\nOK #11 netprobe\n";
        dg_console::acknowledge(ack.data(),ack.size());EXPECT_TRUE(dg_console::ack_pending());
        EXPECT_EQ(dg_console::stats().full,before.full);pump();
        EXPECT_EQ(wire.substr(wire.size()-ack.size()),ack);EXPECT_FALSE(dg_console::ack_pending());
        EXPECT_EQ(dg_console::stats().transport_drops,before.transport_drops);
    }else{
        // Qualified USB retains its best-effort byte/admission path.
        dg_console::acknowledge("OK #11 netprobe\n",16);pump();EXPECT_EQ(wire.find("OK #11"),std::string::npos);
    }
}
TEST_F(ConsoleOutput,ReservedAckFollowsEarlierResponsesAndPrecedesLaterLogs){
    if(!TEST_P4)return;
    EXPECT_TRUE(dg_console::write("PERF {}\n",8));
    EXPECT_TRUE(dg_console::write("STACK task=console\n",19));
    dg_console::acknowledge("\nOK #7 stats\n",13);
    EXPECT_TRUE(dg_console::write("later log\n",10));pump();
    EXPECT_EQ(wire,"PERF {}\nSTACK task=console\n\nOK #7 stats\nlater log\n");
}
TEST_F(ConsoleOutput,OneCommandCanPublishEarlyOkThenErrWithoutUsingTheBulkRing){
    if(!TEST_P4)return;
    dg_console::acknowledge("\nOK #1 probe\n",13);dg_console::acknowledge("\nERR #1 probe\n",14);
    EXPECT_EQ(dg_console::stats().ack_pending,2u);EXPECT_TRUE(dg_console::ack_pending());pump();
    EXPECT_EQ(wire,"\nOK #1 probe\n\nERR #1 probe\n");EXPECT_FALSE(dg_console::ack_pending());
}
TEST_F(ConsoleOutput,UartAckKeepsItsCursorAcrossAnExtendedTransportStall){
    if(!TEST_P4)return;
    const std::string ack="\nERR #9 test\n";const auto before=dg_console::stats();
    dg_console::acknowledge(ack.data(),ack.size());block_after=3;
    EXPECT_THROW(pump(),Stalled);EXPECT_EQ(wire,ack.substr(0,3));
    EXPECT_TRUE(dg_console::ack_pending());EXPECT_EQ(dg_console::stats().transport_drops,before.transport_drops);
    block_after=SIZE_MAX;pump();EXPECT_EQ(wire,ack);EXPECT_FALSE(dg_console::ack_pending());
}
TEST_F(ConsoleOutput,PacedExportsAndChecksKeepUsbGoldenBytesAndNeverFillTheSmallRing){
    struct Export {const char* prefix;const char* row;const char* begin;const char* end;const char* digest;};
    const Export exports[]={
        {"AEC_DUMP","AEC_PCM","AEC_DUMP BEGIN frames=6400 channels=3 rate=16000 order=mic,ref,processed adapter_delay=1024 measured_delay=unknown truncated=0 ns=0 agc=0","AEC_DUMP END frames=6400 timing_blocks=0","bb1f89bed1e726dfc81caa577cd3e4e791ddee2736bbe3f36bb4dc96cf6df7ec"},
        {"MIC_DUMP","MIC_PCM","MIC_DUMP BEGIN samples=19200 rate=16000 raw=1 dsp_bypass=1","MIC_DUMP END samples=19200","39f72d035f5bc04df36f8514a69d3966ea5c27553e46bc10c8567f7fcb542143"},
        {"DUMP","PCM","DUMP BEGIN samples=19200","DUMP END samples=19200","d064f72aab49d7eeb5129710b99c770b0693664245e3c32078e997db27d0c5ef"},
        {"ARRIVALS","ARRIVAL","ARRIVALS BEGIN version=1 record_bytes=32","ARRIVALS END","ec198cd909c3f223a258db83734e398f4348c2b629b7a666a3801fa8b9d18042"},
        {"NETPROBE_DUMP","NETPROBE_ROW","NETPROBE_DUMP BEGIN version=1 record_bytes=16","NETPROBE_DUMP END","a7dc019f5012d8f059b05eadb216327c94b10060aa48d90fb3eb9ec8d807459e"},
    };
    for(const auto& e:exports){
        ring={};wire.clear();now=0;fifo=64;const auto before=dg_console::stats();
        const bool arrival=std::string(e.prefix)=="ARRIVALS",network=std::string(e.prefix)=="NETPROBE_DUMP";
        const std::string payload=arrival?std::string(683,'A')+"=":std::string(512,'A');
        std::string expected=std::string(e.begin)+"\n";dg_console::DumpCheck check;
        const auto begin="\n"+std::string(e.begin)+"\n";dg_console::dump_line(begin.c_str(),output);
        for(unsigned i=0;i<100;++i){
            const std::string row=std::string(e.row)+" "+(arrival || network?"":std::to_string(i*192)+" ")+payload;
            const auto framed="\n"+row+"\n";if(!network || TEST_P4)check.record(framed.c_str());
            dg_console::dump_line(framed.c_str(),output,std::string(e.prefix)=="AEC_DUMP"?8:4);expected+=row+"\n";
        }
        if(!network || TEST_P4){
            char text[128];check.finish(e.prefix,text,sizeof text);dg_console::dump_line(text,output);
            expected+=std::string(e.prefix)+" CHECK lines=100 sha256="+e.digest+"\n";
        }
        const auto end="\n"+std::string(e.end)+"\n";dg_console::dump_line(end.c_str(),output);expected+=std::string(e.end)+"\n";
        const std::string ack=std::string(TEST_P4?"\n":"")+"OK #11 netprobe\n";
        dg_console::acknowledge(ack.data(),ack.size());expected+=ack;pump();
        EXPECT_EQ(wire,expected)<<e.prefix;EXPECT_EQ(dg_console::stats().full,before.full)<<e.prefix;
        EXPECT_EQ(dg_console::stats().transport_drops,before.transport_drops)<<e.prefix;
        if(const auto* directory=std::getenv("CONSOLE_FIXTURE_DIR")){
            std::ofstream(std::string(directory)+(TEST_P4?"/uart-":"/usb-")+e.prefix+".log")<<wire;
        }
    }
}
TEST_F(ConsoleOutput,UsbFramingHasZeroDelayAndUartAlwaysUsesItsWireBudget){
    dg_console::dump_line("\nTAIL\n",output);
    EXPECT_EQ(pauses,TEST_P4?1u:0u);if(TEST_P4){EXPECT_GE(now,1000);}pump();EXPECT_EQ(wire,"TAIL\n");
}
