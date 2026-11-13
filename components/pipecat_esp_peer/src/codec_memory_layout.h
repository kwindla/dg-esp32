#pragma once
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef struct {void *pointer;size_t bytes;} CodecLayoutRow;
typedef struct {
    CodecLayoutRow rows[16];
    unsigned role,count;
    int64_t begin_us;
} CodecLayoutContext;
void pipecat_codec_layout_begin(CodecLayoutContext*,unsigned role);
void pipecat_codec_layout_end(CodecLayoutContext*);
#ifdef __cplusplus
}
#endif
