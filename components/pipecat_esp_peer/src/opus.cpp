#include "opus.h"
#include "esp_opus_enc.h"
#include "esp_opus_dec.h"
#include <stdexcept>
#include "sdkconfig.h"
#if CONFIG_DG_CODEC_LAYOUT_TRACE
#include "codec_memory_layout.h"
#endif
extern "C" void pipecat_codec_memory_init(void);
namespace pipecat::esp_peer {
OpusEncoder::OpusEncoder(unsigned bitrate, unsigned complexity, bool fec, bool dtx, unsigned frame_ms) {
    if (frame_ms != 20 && frame_ms != 40) throw std::invalid_argument("Unsupported uplink frame duration");
    pipecat_codec_memory_init();
    esp_opus_enc_config_t cfg = ESP_OPUS_ENC_CONFIG_DEFAULT();
    cfg.sample_rate = 16000; cfg.channel = 1; cfg.bits_per_sample = 16;
    cfg.bitrate = bitrate; cfg.complexity = complexity;
    cfg.frame_duration = frame_ms == 40 ? ESP_OPUS_ENC_FRAME_DURATION_40_MS : ESP_OPUS_ENC_FRAME_DURATION_20_MS;
    pcm_bytes_ = frame_ms * 16 * sizeof(int16_t);
    cfg.enable_fec = fec; cfg.enable_dtx = dtx;
#if CONFIG_DG_CODEC_LAYOUT_TRACE
    CodecLayoutContext layout;pipecat_codec_layout_begin(&layout,1);
#endif
    auto error=esp_opus_enc_open(&cfg,sizeof(cfg),&handle_);
#if CONFIG_DG_CODEC_LAYOUT_TRACE
    pipecat_codec_layout_end(&layout);
#endif
    if(error)throw std::runtime_error("Opus encoder open failed");
    settings_={int(cfg.sample_rate),int(cfg.channel),int(frame_ms),cfg.bitrate,cfg.complexity,
        int(cfg.application_mode),int(cfg.bits_per_sample),cfg.enable_fec,cfg.enable_dtx,cfg.enable_vbr};
}
OpusEncoder::~OpusEncoder() { if (handle_) esp_opus_enc_close(handle_); }
size_t OpusEncoder::encode(const int16_t* pcm, uint8_t* output, size_t capacity) {
    esp_audio_enc_in_frame_t in{};
    in.buffer = reinterpret_cast<uint8_t*>(const_cast<int16_t*>(pcm)); in.len = pcm_bytes_;
    esp_audio_enc_out_frame_t out{}; out.buffer = output; out.len = capacity;
    if (esp_opus_enc_process(handle_, &in, &out)) throw std::runtime_error("Opus encode failed");
    return out.encoded_bytes;
}
OpusDecoder::OpusDecoder() {
    pipecat_codec_memory_init();
    esp_opus_dec_cfg_t cfg{};
    cfg.sample_rate = 16000; cfg.channel = 1;
    cfg.frame_duration = ESP_OPUS_DEC_FRAME_DURATION_20_MS;
#if CONFIG_DG_CODEC_LAYOUT_TRACE
    CodecLayoutContext layout;pipecat_codec_layout_begin(&layout,2);
#endif
    auto error=esp_opus_dec_open(&cfg,sizeof(cfg),&handle_);
#if CONFIG_DG_CODEC_LAYOUT_TRACE
    pipecat_codec_layout_end(&layout);
#endif
    if(error)throw std::runtime_error("Opus decoder open failed");
}
OpusDecoder::~OpusDecoder() { if (handle_) esp_opus_dec_close(handle_); }
void OpusDecoder::reset() {if(esp_opus_dec_reset(handle_))throw std::runtime_error("Opus decoder reset failed");}
size_t OpusDecoder::decode(uint8_t* packet, size_t bytes, int16_t* output, size_t capacity, bool plc) {
    esp_audio_dec_in_raw_t in{};
    // The vendor wrapper checks buffer != nullptr even for PLC (zero length).
    uint8_t placeholder = 0;
    in.buffer = plc ? &placeholder : packet; in.len = bytes;
    if (plc) in.frame_recover = ESP_AUDIO_DEC_RECOVERY_PLC;
    esp_audio_dec_out_frame_t out{};
    out.buffer = reinterpret_cast<uint8_t*>(output); out.len = capacity * 2;
    esp_audio_dec_info_t info{};
    int err = esp_opus_dec_decode(handle_, &in, &out, &info);
    if (err || (!plc && in.consumed != bytes) || out.decoded_size != 640 ||
        info.sample_rate != 16000 || info.channel != 1)
        throw std::runtime_error("Opus decode format/error");
    return out.decoded_size / 2;
}
}
