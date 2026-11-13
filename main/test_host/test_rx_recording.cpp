#include <gtest/gtest.h>
#include "rx_recording.h"
#include <atomic>
#include <thread>
#include <vector>
namespace {
std::vector<int16_t> filled(size_t n,int16_t value){return std::vector<int16_t>(n,value);}
}
TEST(RxRecording, AppendsUpToCapacityAndDropsWhatDoesNotFit) {
    std::vector<int16_t> storage(1000);dg::RxRecording rx(storage.data(),storage.size());
    rx.append(filled(600,1).data(),600);EXPECT_EQ(rx.samples(),600u);
    rx.append(filled(600,2).data(),600);EXPECT_EQ(rx.samples(),600u);  // A block that does not fit is dropped whole.
    rx.append(filled(400,3).data(),400);EXPECT_EQ(rx.samples(),1000u);
    const auto shot=rx.snapshot();int16_t out[2];
    ASSERT_TRUE(rx.read(shot,599,out,2));EXPECT_EQ(out[0],1);EXPECT_EQ(out[1],3);
    EXPECT_FALSE(rx.read(shot,999,out,2));EXPECT_FALSE(rx.read(shot,1001,out,0));
    EXPECT_TRUE(rx.read(shot,1000,out,0));
}
TEST(RxRecording, AnExportSeesItsOwnPrefixWhileTheSameRecordingGrows) {
    std::vector<int16_t> storage(1000);dg::RxRecording rx(storage.data(),storage.size());
    rx.append(filled(300,7).data(),300);
    const auto shot=rx.snapshot();EXPECT_EQ(shot.samples,300u);
    rx.append(filled(300,8).data(),300);  // Appending leaves the exported prefix intact.
    std::vector<int16_t> out(300);
    ASSERT_TRUE(rx.read(shot,0,out.data(),300));
    for(auto v:out)EXPECT_EQ(v,7);
    EXPECT_FALSE(rx.read(shot,300,out.data(),1));  // Samples after the snapshot are not part of this export.
}
TEST(RxRecording, AResetDuringAnExportIsRefusedInsteadOfMixingTwoRecordings) {
    std::vector<int16_t> storage(1000);dg::RxRecording rx(storage.data(),storage.size());
    rx.append(filled(600,1).data(),600);
    const auto shot=rx.snapshot();
    std::vector<int16_t> out(192);
    ASSERT_TRUE(rx.read(shot,0,out.data(),192));
    for(auto v:out)EXPECT_EQ(v,1);
    // A reconnect resets the recording and the next session overwrites the prefix.
    rx.reset();rx.append(filled(600,2).data(),600);
    EXPECT_FALSE(rx.read(shot,192,out.data(),192));
    EXPECT_FALSE(rx.read(shot,0,out.data(),192));
    for(auto v:out)EXPECT_EQ(v,1);  // A refused read copies nothing.
    // A reset back to the same length is still another recording.
    const auto next=rx.snapshot();EXPECT_NE(next.generation,shot.generation);EXPECT_EQ(next.samples,shot.samples);
    ASSERT_TRUE(rx.read(next,0,out.data(),192));
    for(auto v:out)EXPECT_EQ(v,2);
}
TEST(RxRecording, ConcurrentResetsNeverLeakAnotherRecordingIntoAnExport) {
    std::vector<int16_t> storage(4096);dg::RxRecording rx(storage.data(),storage.size());
    std::atomic<bool> stop{false};
    // Each recording consists only of its own number, so a mixed chunk is visible.
    std::thread producer([&]{
        for(int16_t recording=1;!stop;++recording==30000?recording=1:0){
            rx.reset();
            const auto block=filled(64,recording);
            for(unsigned n=0;n<48 && !stop;++n)rx.append(block.data(),64);
        }
    });
    unsigned complete=0,refused=0;
    for(unsigned attempt=0;attempt<20000;++attempt){
        const auto shot=rx.snapshot();if(shot.samples<192)continue;
        int16_t chunk[192];bool whole=true;int16_t identity=0;
        for(size_t at=0;at+192<=shot.samples && whole;at+=192){
            if(!rx.read(shot,at,chunk,192)){whole=false;break;}
            if(!identity)identity=chunk[0];
            for(auto v:chunk)ASSERT_EQ(v,identity) << "an export mixed two recordings";
        }
        whole?++complete:++refused;
    }
    stop=true;producer.join();
    EXPECT_GT(complete+refused,0u);
}
