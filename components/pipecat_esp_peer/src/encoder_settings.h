#pragma once
namespace pipecat::esp_peer {
struct EncoderSettings {
    int sample_rate=0,channels=0,frame_ms=0,bitrate=0,complexity=0,application=0,bits_per_sample=0;
    bool fec=false,dtx=false,vbr=false;
};
}
