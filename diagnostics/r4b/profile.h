#pragma once
#include <cstddef>
#include <cstdint>
// Included only by the diagnostic overlay; production files are not edited.
enum R4bStage : unsigned { R4bPeerMain, R4bDtlsRead, R4bRecordDecrypt,
    R4bSctp, R4bDataCallback, R4bClassifier, R4bJsonParse, R4bRtviConvert,
    R4bRtviDispatch, R4bAppQueue, R4bAppHandle, R4bLogEnqueue, R4bLogSink,
    R4bAes, R4bSha, R4bCryptoSetup, R4bCryptoWait, R4bCacheSync,
    R4bSocketRecv, R4bSocketSend, R4bInterruptedIsr, R4bNativeQueue, R4bSslRead,
    R4bClientCallback, R4bClientQueue, R4bStageCount };
struct R4bToken { uint32_t cycle=0, previous=0; int64_t time=0; void* slot=nullptr;
    unsigned stage=0,core=0,previous_primary=31; };
R4bToken r4b_enter(unsigned);
void r4b_leave(R4bToken) noexcept;
struct R4bScope { R4bToken t; explicit R4bScope(unsigned s):t(r4b_enter(s)){}
    ~R4bScope(){r4b_leave(t);} };
#define R4B_SCOPE(s) R4bScope r4b_scope_##s(s)
void r4b_init();
void r4b_command(const char*);
void r4b_dead(unsigned site,uint64_t wait_us,uintptr_t caller=0) noexcept;
void r4b_frame(size_t bytes,bool dispatched=false) noexcept;
void r4b_crypto_transfer(unsigned peripheral,bool tx,uint64_t psram,uint64_t internal) noexcept;
extern "C" void r4b_display_snapshot(uint64_t out[13]) noexcept;
