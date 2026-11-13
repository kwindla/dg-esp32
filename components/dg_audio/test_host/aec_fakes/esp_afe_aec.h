#pragma once
#include <cstdint>
#include <cstddef>
constexpr int AFE_TYPE_FD=3,AFE_MODE_LOW_COST=0;
constexpr int AEC_MODE_FD_LOW_COST=5;
struct aec_handle_t {void* aec_handle;};
struct afe_pcm_config_t {int total_ch_num,mic_num;uint8_t* mic_ids;int ref_num;uint8_t* ref_ids;int sample_rate;};
struct afe_aec_handle_t {aec_handle_t* handle;int mode;afe_pcm_config_t pcm_config;int frame_size;int16_t* data;};
afe_aec_handle_t* afe_aec_create(const char*,int,int,int);
int afe_aec_get_chunksize(afe_aec_handle_t*);
size_t afe_aec_process(afe_aec_handle_t*,const int16_t*,int16_t*);
void afe_aec_destroy(afe_aec_handle_t*);
