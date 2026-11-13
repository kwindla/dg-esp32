#pragma once
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <stdexcept>
#include <string>
namespace dg {
// Receives a 16 kHz mono PCM16 fixture over the console and checks it before
// it may stand in for the microphone. Chunks of whole samples arrive in order
// as base64 text; the end names the sample count and the CRC-32 (the zlib
// one) of the little-endian PCM bytes. The caller owns the storage.
//
// A console can lose part of a long line without saying so (the S3's
// USB-Serial-JTAG driver drops a 64-byte packet that does not fit its receive
// ring), and what is left of a base64 line usually still decodes to whole
// samples. So every data line names its own sample count and CRC-32, and a
// line that does not match them is refused. Nothing of a refused line is
// kept: the same line can be sent again.
class FixtureLoad {
public:
    static constexpr size_t max_samples=16000*30;
    void begin(int16_t* storage,size_t samples) {
        clear();
        if(!storage || !samples || samples>max_samples)throw std::runtime_error("fixture is 1..480000 samples");
        storage_=storage;samples_=samples;
    }
    // The arguments of one mic-load-data line: "<sample offset> <samples> <crc32> <base64>".
    void data_line(const char* arguments) {
        // Each number must be followed by a space, and the base64 must be there:
        // a field that ran into its neighbour is no field.
        unsigned long long field[3]{};const char* p=arguments;
        for(unsigned i=0;i<3;++i) {
            while(*p==' ')++p;
            char* end=nullptr;
            field[i]=std::strtoull(p,&end,i==2?16:10);
            if(end==p || *end!=' ' || *p=='-' || *p=='+' || field[i]>0xffffffffull)
                throw std::runtime_error("mic-load-data <sample offset> <samples> <crc32> <base64>");
            p=end;
        }
        while(*p==' ')++p;
        if(!*p)throw std::runtime_error("mic-load-data <sample offset> <samples> <crc32> <base64>");
        data(size_t(field[0]),size_t(field[1]),uint32_t(field[2]),p);
    }
    void data(size_t offset,size_t samples,uint32_t crc,const char* base64) {
        if(!storage_ || complete_)throw std::runtime_error("fixture load not begun");
        if(offset!=bytes_/2)throw std::runtime_error("fixture chunk out of order: the next sample offset is "+std::to_string(bytes_/2));
        auto* at=reinterpret_cast<uint8_t*>(storage_)+bytes_;
        const size_t n=decode(base64,at,samples_*2-bytes_);
        if(!samples || n%2 || n/2!=samples)
            throw std::runtime_error("fixture chunk decodes to "+std::to_string(n)+" bytes, not the "+
                std::to_string(uint64_t(samples)*2)+" its line promises: the line arrived cut short or damaged");
        if(crc32(at,n)!=crc)throw std::runtime_error("fixture chunk CRC mismatch: the line arrived damaged");
        bytes_+=n;
    }
    size_t end(size_t samples,uint32_t crc) {
        if(!storage_ || complete_ || samples!=samples_ || bytes_!=samples_*2)throw std::runtime_error("fixture is incomplete");
        if(crc32(reinterpret_cast<const uint8_t*>(storage_),bytes_)!=crc)throw std::runtime_error("fixture CRC mismatch");
        complete_=true;return samples_;
    }
    void clear()noexcept{storage_=nullptr;samples_=bytes_=0;complete_=false;}
    // Null until a whole, verified fixture is present.
    const int16_t* pcm()const noexcept{return complete_?storage_:nullptr;}
    size_t samples()const noexcept{return complete_?samples_:0;}
    size_t received()const noexcept{return bytes_/2;}
    static uint32_t crc32(const uint8_t* data,size_t n)noexcept {
        uint32_t crc=0xffffffffu;
        for(size_t i=0;i<n;++i){crc^=data[i];for(unsigned bit=0;bit<8;++bit)crc=(crc>>1)^(0xedb88320u&(0u-(crc&1u)));}
        return ~crc;
    }
private:
    // Standard base64 with padding. Returns the bytes written; never writes past `room`.
    static size_t decode(const char* text,uint8_t* out,size_t room) {
        const auto value=[](char c)->int {
            if(c>='A' && c<='Z')return c-'A';
            if(c>='a' && c<='z')return c-'a'+26;
            if(c>='0' && c<='9')return c-'0'+52;
            return c=='+'?62:c=='/'?63:-1;
        };
        size_t written=0;uint32_t group=0;unsigned have=0;bool padded=false;
        for(const char* p=text;*p && *p!=' ' && *p!='\r' && *p!='\n';++p) {
            if(*p=='='){padded=true;continue;}
            const int v=value(*p);
            if(v<0 || padded)throw std::runtime_error("fixture chunk is not base64");
            group=(group<<6)|unsigned(v);
            if(++have==4) {
                if(written+3>room)throw std::runtime_error("fixture chunk overflows the fixture");
                out[written++]=uint8_t(group>>16);out[written++]=uint8_t(group>>8);out[written++]=uint8_t(group);
                group=0;have=0;
            }
        }
        if(have==1)throw std::runtime_error("fixture chunk is not base64");
        if(have) {
            const size_t tail=have-1;
            if(written+tail>room)throw std::runtime_error("fixture chunk overflows the fixture");
            group<<=6*(4-have);
            out[written++]=uint8_t(group>>16);
            if(tail==2)out[written++]=uint8_t(group>>8);
        }
        return written;
    }
    int16_t* storage_=nullptr;size_t samples_=0,bytes_=0;bool complete_=false;
};
}
