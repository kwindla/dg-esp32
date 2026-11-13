#include "dg_audio/aec_framer.h"
#include <gtest/gtest.h>
#include <array>
#include <vector>
using dg_audio::AecFramer;

namespace {
struct Identity {
    size_t chunk;unsigned consumed=0;
    static int16_t signal(unsigned i) {return int16_t(1+(i*71)%30000);}
    static size_t process(void* context,const int16_t* input,int16_t* output) {
        auto& s=*static_cast<Identity*>(context);
        for(size_t i=0;i<s.chunk;++i) {
            EXPECT_EQ(input[2*i],signal(s.consumed));
            EXPECT_EQ(input[2*i+1],-signal(s.consumed));
            output[i]=input[2*i];++s.consumed;
        }
        return 2*s.chunk;
    }
};
}

TEST(AecFramer, ArbitraryNativeChunksKeepConstantDelayWithoutDropsOrInserts) {
    for(size_t chunk:{80u,160u,256u,320u,480u,512u,640u,1024u}) {
        std::vector<int16_t> input(2*chunk),processed(chunk),fifo(AecFramer::fifo_samples(chunk));
        Identity identity{chunk};AecFramer framer(chunk,input.data(),processed.data(),fifo.data(),Identity::process,&identity);
        std::array<int16_t,320> mic,ref,out;
        unsigned at=0;
        // Frame boundaries and session/turn boundaries cannot reset adapter
        // state. Run 400 blocks, including partial IO calls and many wraps.
        for(unsigned block=0;block<400;++block) {
            const auto n=block%3==0?128u:block%3==1?320u:192u;
            for(unsigned i=0;i<n;++i) {mic[i]=Identity::signal(at+i);ref[i]=-mic[i];}
            ASSERT_TRUE(framer.run(mic.data(),ref.data(),n,out.data()));
            for(unsigned i=0;i<n;++i)EXPECT_EQ(out[i],at+i<chunk?0:Identity::signal(at+i-chunk));
            at+=n;
        }
        EXPECT_EQ(framer.input_samples(),at);EXPECT_EQ(framer.output_samples(),at);
        EXPECT_EQ(framer.calls(),at/chunk);EXPECT_EQ(framer.errors(),0u);
    }
}

TEST(AecFramer, ProcessorShortResultIsAnExplicitErrorWithNoStalePayload) {
    constexpr unsigned chunk=320;
    std::array<int16_t,chunk*2> input;std::array<int16_t,chunk> processed,out,mic,ref;
    std::vector<int16_t> fifo(AecFramer::fifo_samples(chunk));mic.fill(100);ref.fill(200);processed.fill(12345);
    AecFramer framer(chunk,input.data(),processed.data(),fifo.data(),[](void*,const int16_t*,int16_t*) -> size_t {return 0;},nullptr);
    EXPECT_FALSE(framer.run(mic.data(),ref.data(),chunk,out.data()));
    EXPECT_EQ(framer.errors(),1u);for(auto sample:out)EXPECT_EQ(sample,0);
    EXPECT_FALSE(framer.run(mic.data(),ref.data(),chunk,out.data()));
    EXPECT_EQ(framer.errors(),2u);for(auto sample:out)EXPECT_EQ(sample,0);
}

TEST(AecFramer, InvalidFrameCannotWriteOutputOrConsumeInput) {
    std::array<int16_t,640> input;std::array<int16_t,320> processed,out,mic,ref;
    std::vector<int16_t> fifo(AecFramer::fifo_samples(320));Identity identity{320};out.fill(9);
    AecFramer framer(320,input.data(),processed.data(),fifo.data(),Identity::process,&identity);
    EXPECT_FALSE(framer.run(mic.data(),ref.data(),321,out.data()));
    EXPECT_EQ(identity.consumed,0u);EXPECT_EQ(framer.input_samples(),0u);
    for(auto value:out)EXPECT_EQ(value,9);
}
