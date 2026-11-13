#include <gtest/gtest.h>
#include "fixture_load.h"
#include <cstdio>
#include <string>
#include <vector>
namespace {
std::string base64(const uint8_t* data,size_t n) {
    static const char table[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    for(size_t i=0;i<n;i+=3) {
        const uint32_t group=(uint32_t(data[i])<<16)|(i+1<n?uint32_t(data[i+1])<<8:0)|(i+2<n?data[i+2]:0);
        out+=table[group>>18];out+=table[(group>>12)&63];
        out+=i+1<n?table[(group>>6)&63]:'=';out+=i+2<n?table[group&63]:'=';
    }
    return out;
}
std::string chunk(const std::vector<int16_t>& pcm,size_t at,size_t n) {
    return base64(reinterpret_cast<const uint8_t*>(pcm.data()+at),n*2);
}
std::vector<int16_t> ramp(size_t n){std::vector<int16_t> pcm(n);for(size_t i=0;i<n;++i)pcm[i]=int16_t(i*37-9000);return pcm;}
uint32_t crc(const std::vector<int16_t>& pcm){return dg::FixtureLoad::crc32(reinterpret_cast<const uint8_t*>(pcm.data()),pcm.size()*2);}
uint32_t crc(const std::vector<int16_t>& pcm,size_t at,size_t n){return dg::FixtureLoad::crc32(reinterpret_cast<const uint8_t*>(pcm.data()+at),n*2);}
// The arguments of the line scripts/mic_fixture_lines.py writes for these samples.
std::string line(const std::vector<int16_t>& pcm,size_t at,size_t n) {
    char head[64];std::snprintf(head,sizeof head," %zu %zu %08x ",at,n,unsigned(crc(pcm,at,n)));
    return head+chunk(pcm,at,n);
}
std::string refusal(dg::FixtureLoad& load,const std::string& arguments) {
    try {load.data_line(arguments.c_str());}catch(const std::runtime_error& error){return error.what();}
    return "accepted";
}
}
TEST(FixtureLoad, CrcIsTheZlibOne) {
    const uint8_t check[]={'1','2','3','4','5','6','7','8','9'};
    EXPECT_EQ(dg::FixtureLoad::crc32(check,9),0xcbf43926u);
    EXPECT_EQ(dg::FixtureLoad::crc32(check,0),0u);
}
// The three lines scripts/mic_fixture_lines.py prints for the samples 1, -2, 3, -4.
// scripts/test_mic_fixture_lines.py holds the script to the same text.
TEST(FixtureLoad, AcceptsTheLinesTheScriptWrites) {
    std::vector<int16_t> storage(4);dg::FixtureLoad load;
    load.begin(storage.data(),4);                      // mic-load-begin 4
    load.data_line(" 0 4 cbca8ec0 AQD+/wMA/P8=");      // mic-load-data 0 4 cbca8ec0 AQD+/wMA/P8=
    EXPECT_EQ(load.end(4,0xcbca8ec0u),4u);             // mic-load-end 4 cbca8ec0
    EXPECT_EQ(storage,(std::vector<int16_t>{1,-2,3,-4}));
}
TEST(FixtureLoad, OrderedChunksOfAnySizeGiveTheSamePcmAndOnlyAVerifiedFixtureIsOffered) {
    const auto pcm=ramp(4001);
    for(size_t step:{1u,2u,750u,1536u,4001u}) {
        std::vector<int16_t> storage(pcm.size(),int16_t(-1));dg::FixtureLoad load;
        load.begin(storage.data(),pcm.size());
        for(size_t at=0;at<pcm.size();at+=step) {
            EXPECT_EQ(load.pcm(),nullptr);EXPECT_EQ(load.samples(),0u);
            load.data_line(line(pcm,at,std::min(step,pcm.size()-at)).c_str());
        }
        EXPECT_EQ(load.received(),pcm.size());EXPECT_EQ(load.pcm(),nullptr);
        EXPECT_EQ(load.end(pcm.size(),crc(pcm)),pcm.size());
        EXPECT_EQ(load.pcm(),storage.data());EXPECT_EQ(load.samples(),pcm.size());
        EXPECT_EQ(storage,pcm) << "step " << step;
    }
}
TEST(FixtureLoad, RefusesWhatIsNotTheWholeFixtureItWasPromised) {
    const auto pcm=ramp(300);std::vector<int16_t> storage(300);dg::FixtureLoad load;
    EXPECT_EQ(refusal(load,line(pcm,0,10)),"fixture load not begun");
    EXPECT_THROW(load.begin(storage.data(),0),std::runtime_error);
    EXPECT_THROW(load.begin(storage.data(),dg::FixtureLoad::max_samples+1),std::runtime_error);
    EXPECT_THROW(load.begin(nullptr,300),std::runtime_error);
    load.begin(storage.data(),300);
    EXPECT_EQ(refusal(load,line(pcm,10,10)),"fixture chunk out of order: the next sample offset is 0");  // A gap.
    load.data_line(line(pcm,0,100).c_str());
    EXPECT_EQ(refusal(load,line(pcm,0,100)),"fixture chunk out of order: the next sample offset is 100");  // A repeat.
    const uint32_t zero=dg::FixtureLoad::crc32(reinterpret_cast<const uint8_t*>("\0\0\0"),3);
    EXPECT_THROW(load.data(100,1,zero,"AA"),std::runtime_error);  // One byte: not a whole sample.
    EXPECT_THROW(load.data(100,1,zero,"AAAA"),std::runtime_error);  // Three bytes.
    EXPECT_THROW(load.data(100,1,zero,"A"),std::runtime_error);  // Six bits are no byte at all.
    EXPECT_THROW(load.data(100,1,zero,"AA=A"),std::runtime_error);
    EXPECT_THROW(load.data(100,1,zero,"A*AA"),std::runtime_error);
    EXPECT_THROW(load.data(100,0,0,""),std::runtime_error);  // No samples is no chunk.
    // A line must have all four arguments, and the old two-argument line is not accepted.
    for(const char* bad:{""," 100"," 100 1"," 100 1 00000000"," 100 1 00000000 "," 100 AAA="," x 1 00000000 AAA=",
                         " 100 1AAA= 00000000 AAA="," -100 1 00000000 AAA="," 100 1 100000000 AAA="," 100 1 0000000g AAA="})
        EXPECT_EQ(refusal(load,bad),"mic-load-data <sample offset> <samples> <crc32> <base64>") << bad;
    EXPECT_EQ(load.received(),100u);
    EXPECT_THROW(load.end(300,crc(pcm)),std::runtime_error);  // Incomplete.
    load.data_line(line(pcm,100,200).c_str());
    // Past the promised length.
    EXPECT_THROW(load.data(300,1,crc(pcm,0,1),chunk(pcm,0,1).c_str()),std::runtime_error);
    EXPECT_THROW(load.end(299,crc(pcm)),std::runtime_error);
    EXPECT_THROW(load.end(300,crc(pcm)^1u),std::runtime_error);
    EXPECT_EQ(load.pcm(),nullptr);
    EXPECT_EQ(load.end(300,crc(pcm)),300u);
    EXPECT_THROW(load.data(300,1,crc(pcm,0,1),chunk(pcm,0,1).c_str()),std::runtime_error);  // Finished.
    EXPECT_THROW(load.end(300,crc(pcm)),std::runtime_error);
    load.clear();EXPECT_EQ(load.pcm(),nullptr);EXPECT_EQ(load.received(),0u);
}
TEST(FixtureLoad, AChunkNeverWritesPastTheFixture) {
    const auto pcm=ramp(64);std::vector<int16_t> storage(40,int16_t(0x5a5a));dg::FixtureLoad load;
    load.begin(storage.data(),32);  // Eight samples of storage must stay untouched.
    EXPECT_EQ(refusal(load,line(pcm,0,64)),"fixture chunk overflows the fixture");
    for(size_t i=32;i<40;++i)EXPECT_EQ(storage[i],int16_t(0x5a5a));
    EXPECT_EQ(load.received(),0u);
    load.data_line(line(pcm,0,32).c_str());
    for(size_t i=32;i<40;++i)EXPECT_EQ(storage[i],int16_t(0x5a5a));
    // A following line may carry further console text after the payload.
    dg::FixtureLoad second;std::vector<int16_t> other(4);second.begin(other.data(),4);
    second.data_line((line(pcm,0,4)+" trailing").c_str());EXPECT_EQ(second.received(),4u);
}
// Found on the S3: its console lost 64-byte packets of a long line, what was
// left still decoded to whole samples, the chunk was accepted short, and the
// load failed one line later with nothing to say why.
TEST(FixtureLoad, ALineThatLostPartOfItsTextIsRefusedAndCanBeSentAgain) {
    const auto pcm=ramp(1536);std::vector<int16_t> storage(pcm.size());dg::FixtureLoad load;
    load.begin(storage.data(),pcm.size());
    const auto whole=line(pcm,0,1536);
    const auto payload=whole.rfind(' ')+1;
    ASSERT_EQ(whole.size()-payload,4096u);
    // One packet gone from the middle: 64 characters are 48 bytes, so 1512 whole samples remain.
    auto cut=whole;cut.erase(payload+1000,64);
    EXPECT_EQ(refusal(load,cut),"fixture chunk decodes to 3024 bytes, not the 3072 its line promises: the line arrived cut short or damaged");
    // The end gone, as when a line is longer than the console's own buffer.
    EXPECT_EQ(refusal(load,whole.substr(0,payload+2048)),"fixture chunk decodes to 1536 bytes, not the 3072 its line promises: the line arrived cut short or damaged");
    // The right length with one character changed.
    auto damaged=whole;damaged[payload+7]=damaged[payload+7]=='A'?'B':'A';
    EXPECT_EQ(refusal(load,damaged),"fixture chunk CRC mismatch: the line arrived damaged");
    // A packet gone from the front takes the count or the CRC with it.
    auto headless=whole;headless.erase(3,64);
    EXPECT_NE(refusal(load,headless),"accepted");
    // Two lines run together because the first lost its end.
    EXPECT_EQ(refusal(load,whole.substr(0,payload+4000)+"#7 mic-load-data"+line(pcm,0,1)),"fixture chunk is not base64");
    // Nothing was kept from any of them, and the same line is accepted when it arrives whole.
    EXPECT_EQ(load.received(),0u);EXPECT_EQ(load.pcm(),nullptr);
    load.data_line(whole.c_str());
    EXPECT_EQ(load.received(),1536u);
    EXPECT_EQ(load.end(pcm.size(),crc(pcm)),pcm.size());
    EXPECT_EQ(storage,pcm);
}
