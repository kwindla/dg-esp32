#pragma once
#include <array>
#include <cstdint>
#include <cstddef>
#include <string_view>
#include <charconv>
namespace pipecat::esp_peer {
struct RtpAudioView {
    uint32_t ssrc=0, timestamp=0;
    uint16_t sequence=0;
    bool marker=false;
    const uint8_t* payload=nullptr;
    size_t size=0;
};
enum class IngressError { None, Malformed, PayloadType, Duration, Source, Preconnect, Late, Duplicate, Window, Capacity, Generation, Conflict, Count };
struct AudioTrack {
    std::array<bool,128> opus{};
    uint32_t ssrc=0, generation=0;
    // This job supports one signaled remote Opus track. Unknown SSRCs cannot
    // authorize themselves; a later accepted SDP explicitly changes generation.
    bool accepts(uint32_t source) const { return generation && source==ssrc; }
};
inline void audio_track(std::string_view sdp, uint32_t generation, AudioTrack& out) {
    out={};bool audio=false;std::array<bool,128> offered{};
    while(!sdp.empty()) {
        auto end=sdp.find('\n');auto line=sdp.substr(0,end);
        if(!line.empty() && line.back()=='\r')line.remove_suffix(1);
        if(line.substr(0,2)=="m=") {
            audio=line.substr(0,8)=="m=audio ";
            if(audio) {
                auto pt=line.find(' ',8);if(pt!=line.npos)pt=line.find(' ',pt+1);
                while(pt!=line.npos) {
                    line.remove_prefix(pt+1);unsigned value=128;
                    auto r=std::from_chars(line.data(),line.data()+line.size(),value);
                    if(r.ec==std::errc{} && value<128)offered[value]=true;
                    pt=line.find(' ');
                }

            }
        }
        if(audio && line.substr(0,9)=="a=rtpmap:") {
            auto space=line.find(' ');unsigned pt=128;
            auto r=std::from_chars(line.data()+9,line.data()+line.size(),pt);
            if(r.ec==std::errc{} && space!=line.npos && pt<128 && offered[pt] &&
                line.substr(space+1,11)=="opus/48000/")out.opus[pt]=true;
        }
        if(audio && line.substr(0,7)=="a=ssrc:") {
            uint32_t value=0;auto r=std::from_chars(line.data()+7,line.data()+line.size(),value);
            if(r.ec==std::errc{} && !out.ssrc)out.ssrc=value;
        }
        if(end==sdp.npos)break;
        sdp.remove_prefix(end+1);
    }
    if(out.ssrc)out.generation=generation;
}
inline AudioTrack audio_track(std::string_view sdp, uint32_t generation) {
    AudioTrack out;audio_track(sdp,generation,out);return out;
}
inline uint32_t rtp_u32(const uint8_t* p) {return uint32_t(p[0])<<24|uint32_t(p[1])<<16|uint32_t(p[2])<<8|p[3];}
// Validate Opus framing before admitting it. RTP uses 48 kHz regardless of PCM
// decoder rate. We accept exactly 960 ticks, including multi-frame packets.
inline bool opus_20ms(const uint8_t* p,size_t n) {
    if(!p || !n)return false;
    unsigned config=p[0]>>3,code=p[0]&3;
    unsigned ticks=config<12 ? (config%4==3?2880:480u<<(config%4)) :
        config<16 ? 480u<<(config%2) : 120u<<(config%4);
    unsigned count=code==0?1:code<3?2:(n>1?p[1]&63:0);
    if(!count || ticks*count!=960)return false;
    size_t at=1,end=n;
    auto length=[&](size_t& len) {
        if(at>=end)return false;
        unsigned x=p[at++];
        if(x>=252) {if(at>=end)return false;x+=4*p[at++];}
        len=x;return true;
    };
    if(code==0)return n-1<=1275;
    if(code==1)return (n-1)%2==0 && (n-1)/2<=1275;
    if(code==2) {size_t a;if(!length(a) || a>end-at)return false;return a<=1275 && end-at-a<=1275;}
    bool vbr=p[1]&128,padding=p[1]&64;at=2;
    if(padding) {
        size_t pad=0;unsigned x;
        do {if(at>=end)return false;x=p[at++];pad+=x==255?254:x;if(pad>end-at)return false;}while(x==255);
        end-=pad;
    }
    if(!vbr)return end>=at && (end-at)%count==0 && (end-at)/count<=1275;
    size_t sum=0;
    for(unsigned i=1;i<count;++i) {size_t len;if(!length(len) || len>1275)return false;sum+=len;}
    return sum<=end-at && end-at-sum<=1275;
}
inline IngressError parse_audio_rtp(const uint8_t* p,size_t n,const AudioTrack& track,RtpAudioView& out) {
    if(!p || n<12 || p[0]>>6!=2)return IngressError::Malformed;
    if(!track.opus[p[1]&127])return IngressError::PayloadType;
    size_t at=12+4*(p[0]&15),end=n;
    if(at>end)return IngressError::Malformed;
    if(p[0]&16) {
        if(end-at<4)return IngressError::Malformed;
        size_t ext=4+4*(size_t(p[at+2])<<8|p[at+3]);
        if(ext>end-at)return IngressError::Malformed;
        at+=ext;
    }
    if(p[0]&32) {unsigned pad=p[n-1];if(!pad || pad>end-at)return IngressError::Malformed;end-=pad;}
    if(end<=at || end-at>1500)return IngressError::Malformed;
    out={rtp_u32(p+8),rtp_u32(p+4),uint16_t(p[2]<<8|p[3]),bool(p[1]&128),p+at,end-at};
    if(!track.accepts(out.ssrc))return IngressError::Source;
    if(!opus_20ms(out.payload,out.size))return IngressError::Duration;
    return IngressError::None;
}
// Called only by the native receiver transformer, after SRTP authentication.
// SDP authorizes this session's media track; opening the SCTP chat channel is
// an independent handshake and must not discard already arriving audio.
struct AudioAdmission { IngressError error; bool before_chat; };
inline AudioAdmission admit_audio_rtp(const uint8_t* p,size_t n,const AudioTrack& track,
                                     bool retired,bool chat_open,RtpAudioView& out) {
    if(retired)return {IngressError::Generation,false};
    auto reason=parse_audio_rtp(p,n,track,out);
    return {reason,reason==IngressError::None && !chat_open};
}
}
