#include "dg_display/panel_config.h"
#include "dg_display/dgi.h"
#include <cstring>
namespace dg_display {
namespace {
uint16_t be16(const uint8_t* p){return uint16_t(p[0])<<8|p[1];}
uint16_t le16(const uint8_t* p){return uint16_t(p[1])<<8|p[0];}
uint32_t le32(const uint8_t* p){return uint32_t(p[0])|uint32_t(p[1])<<8|uint32_t(p[2])<<16|uint32_t(p[3])<<24;}
unsigned slot(Pixel p){return ((p.color>>11)*3+((p.color>>5)&63)*5+(p.color&31)*7+p.alpha*11)&63;}
bool valid(Pixel p,unsigned mode){return (mode!=0 || p.alpha==255) && (mode!=1 || p.alpha==0 || p.alpha==255) && (p.alpha || !p.color);}
}
uint16_t blend(uint16_t d,Pixel s) noexcept {
    if(!s.alpha)return d;
    if(s.alpha==255)return s.color;
    auto expand=[](unsigned v,unsigned bits){return bits==5?(v<<3)|(v>>2):(v<<2)|(v>>4);};
    auto mix=[&](unsigned a,unsigned b,unsigned bits){unsigned v=(expand(a,bits)*s.alpha+expand(b,bits)*(255-s.alpha)+127)/255;return (v*((1u<<bits)-1)+127)/255;};
    return uint16_t(mix(s.color>>11,d>>11,5)<<11|mix((s.color>>5)&63,(d>>5)&63,6)<<5|mix(s.color&31,d&31,5));
}
bool Image::open(const uint8_t* bytes,size_t size) noexcept {
    if(!bytes || size<40 || size>524288 || std::memcmp(bytes,"DGI1",4) || bytes[4]!=1 || bytes[5]>2 || (bytes[6]!=1 && bytes[6]!=2) || bytes[7])return false;
    Image candidate;candidate.bytes_=bytes;candidate.size_=size;candidate.alpha_=bytes[5];candidate.codec_=bytes[6];
    candidate.width_=le16(bytes+8);candidate.height_=le16(bytes+10);candidate.payload_=le32(bytes+20);
    if(!candidate.width_ || candidate.width_>PanelWidth || !candidate.height_ || candidate.height_>PanelHeight || le32(bytes+12)!=size || le32(bytes+16)!=32 || candidate.payload_!=32+4u*(candidate.height_+1) || candidate.payload_>=size)return false;
    for(unsigned i=24;i<32;++i)if(bytes[i])return false;
    uint32_t previous=0;
    for(unsigned y=0;y<=candidate.height_;++y){auto offset=le32(bytes+32+4*y);if((!y && offset) || (y && offset<=previous) || offset>size-candidate.payload_)return false;previous=offset;}
    if(previous!=size-candidate.payload_)return false;
    for(unsigned y=0;y<candidate.height_;++y)if(!candidate.row(y,0,0,nullptr,true))return false;
    *this=candidate;return true;
}
bool Image::crop(unsigned y,unsigned x,unsigned count,Pixel* out,size_t capacity) const noexcept {
    if(!bytes_ || y>=height_ || x>width_ || count>width_-x || count>capacity || (count && !out))return false;
    return count?row(y,x,count,out,false):true;
}
bool Image::row(unsigned y,unsigned x,unsigned count,Pixel* out,bool full) const noexcept {
    const uint8_t* p=bytes_+payload_+le32(bytes_+32+4*y);
    const uint8_t* end=bytes_+payload_+le32(bytes_+36+4*y);
    if(p>=end)return false;
    unsigned marker=*p++;
    if(marker==0){
        size_t alpha_bytes=alpha_==0?0:alpha_==1?(width_+7)/8:width_;
        if(size_t(end-p)!=2u*width_+alpha_bytes)return false;
        if(alpha_==1 && width_%8 && (end[-1]&((1u<<(8-width_%8))-1)))return false;
        unsigned begin=full?0:x,limit=full?width_:x+count;
        for(unsigned i=begin;i<limit;++i){Pixel px;px.color=be16(p+2*i);px.alpha=alpha_==0?255:alpha_==1?((p[2*width_+i/8]>>(7-i%8))&1)*255:p[2*width_+i];if(!valid(px,alpha_))return false;if(!full)out[i-x]=px;}
        return true;
    }
    if(marker!=1)return false;
    unsigned produced=0;Pixel previous{};Pixel table[64]{};
    for(auto& px:table)px.alpha=0;
    while(p<end && produced<width_){
        Pixel pixel=previous;unsigned run=1;
        if(codec_==1){
            run=*p++;if(!run || size_t(end-p)<size_t(2+(alpha_!=0)))return false;
            pixel.color=be16(p);p+=2;pixel.alpha=alpha_?*p++:255;
        } else {
            unsigned token=*p++;
            if(token<64)pixel=table[token];
            else if(token>=128 && token<192){
                int r=int(previous.color>>11)+int((token>>4)&3)-2;
                int g=int((previous.color>>5)&63)+int((token>>2)&3)-2;
                int b=int(previous.color&31)+int(token&3)-2;
                if(r<0 || r>31 || g<0 || g>63 || b<0 || b>31)return false;
                pixel.color=uint16_t(r<<11|g<<5|b);
            } else if(token>=192 && token<=253)run=(token&63)+1;
            else if(token==254 || token==255){
                if(size_t(end-p)<size_t(token==255?3:2) || (token==255 && !alpha_))return false;
                pixel.color=be16(p);p+=2;if(token==255)pixel.alpha=*p++;
            } else return false;
        }
        if(run>width_-produced || !valid(pixel,alpha_))return false;
        for(unsigned n=0;n<run;++n){
            if(!full && produced>=x && produced<x+count)out[produced-x]=pixel;
            ++produced;previous=pixel;table[slot(pixel)]=pixel;
            if(!full && produced>=x+count)return true;
        }
    }
    return produced==width_ && p==end;
}
}
