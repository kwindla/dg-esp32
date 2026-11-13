#include "dg_dump_check.h"
#include "esp_heap_caps.h"
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <new>
#include <stdexcept>
namespace dg_console {
namespace {
void checked(psa_status_t status) {if(status!=PSA_SUCCESS)throw std::runtime_error("Dump SHA256 failed");}
}
struct DumpCheck::HashState {
    psa_hash_operation_t operation=PSA_HASH_OPERATION_INIT;
    unsigned char scratch[scratch_bytes];
};
DumpCheck::DumpCheck() {
    auto* storage=heap_caps_malloc(sizeof(HashState),MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    if(!storage)throw std::bad_alloc();
    state=new(storage)HashState;
    const auto status=psa_hash_setup(&state->operation,PSA_ALG_SHA_256);
    if(status!=PSA_SUCCESS){
        psa_hash_abort(&state->operation);state->~HashState();heap_caps_free(state);checked(status);
    }
}
DumpCheck::~DumpCheck() {psa_hash_abort(&state->operation);state->~HashState();heap_caps_free(state);}
void DumpCheck::update(const char* text,size_t bytes) {
    // The console stack/strings live in PSRAM. S3 crypto-GDMA reading PSRAM
    // can starve the LCD SPI stream. Both inputs and the operation's pending
    // partial-block buffer must be internal, including during LF updates.
    while(bytes) {
        const auto n=std::min(bytes,scratch_bytes);
        std::memcpy(state->scratch,text,n);
        checked(psa_hash_update(&state->operation,state->scratch,n));text+=n;bytes-=n;
    }
}
void DumpCheck::record(const char* text) {
    if(*text=='\n')++text;
    auto bytes=std::strlen(text);
    if(bytes && text[bytes-1]=='\n')--bytes;
    if(lines)update("\n",1);
    update(text,bytes);
    ++lines;
}
void DumpCheck::finish(const char* prefix,char* output,size_t bytes) {
    size_t written=0;
    checked(psa_hash_finish(&state->operation,state->scratch,scratch_bytes,&written));
    if(written!=32)throw std::runtime_error("Dump SHA256 length");
    char hex[65];
    for(unsigned i=0;i<32;++i)std::snprintf(hex+i*2,3,"%02x",state->scratch[i]);
    const auto n=std::snprintf(output,bytes,"\n%s CHECK lines=%u sha256=%s\n",prefix,lines,hex);
    if(n<0 || size_t(n)>=bytes)throw std::runtime_error("Dump CHECK buffer");
}
}
