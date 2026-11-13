#include "dg_dump_check.h"
#include <openssl/evp.h>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
int main(int argc,char** argv) {
    const std::string prefix=argc>1?argv[1]:"AEC_DUMP";
    const unsigned channels=argc>2?std::atoi(argv[2]):3;
    constexpr unsigned frames=130;
    const auto count=frames*channels;
    std::vector<unsigned char> pcm(count*2);
    for(unsigned i=0;i<pcm.size();++i)pcm[i]=i%251;
    if(prefix=="AEC_DUMP") {
        std::printf("AEC_DUMP BEGIN %sframes=130 channels=%u rate=16000 order=%s adapter_delay=1024 measured_delay=unknown truncated=0 ns=0 agc=0\n",
            channels==5?"schema=2 ":"",channels,channels==5?"mic1,mic2,mic3,mic4,processed":"mic,ref,processed");
        std::puts("AEC_TIME start=0 end=130 lower_us=100 upper_us=200 gap=0");
    }else if(prefix=="ARRIVALS")std::puts("ARRIVALS BEGIN version=1 record_bytes=32");
    else std::printf("%s BEGIN samples=%u%s\n",prefix.c_str(),count,prefix=="MIC_DUMP"?" rate=16000 raw=1 dsp_bypass=1":"");
    const char* record=prefix=="AEC_DUMP"?"AEC_PCM":prefix=="MIC_DUMP"?"MIC_PCM":prefix=="ARRIVALS"?"ARRIVAL":"PCM";
    dg_console::DumpCheck check;
    for(unsigned at=0;at<count;at+=192) {
        unsigned char encoded[513];char line[560];
        const auto bytes=prefix=="ARRIVALS"?32:std::min(192u,count-at)*2;
        const int n=EVP_EncodeBlock(encoded,pcm.data()+at*2,bytes);
        if(prefix=="ARRIVALS")std::snprintf(line,sizeof line,"\n%s %.*s\n",record,n,encoded);
        else std::snprintf(line,sizeof line,"\n%s %u %.*s\n",record,at,n,encoded);
        check.record(line);std::fputs(line+1,stdout);
    }
    char output[128];check.finish(prefix.c_str(),output,sizeof output);std::fputs(output+1,stdout);
    if(prefix=="AEC_DUMP")std::puts("AEC_DUMP END frames=130 timing_blocks=1");
    else if(prefix=="ARRIVALS")std::puts("ARRIVALS END");
    else std::printf("%s END samples=%u\n",prefix.c_str(),count);
}
