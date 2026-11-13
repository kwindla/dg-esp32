#pragma once
#include "psa/crypto.h"
#include <cstddef>
namespace dg_console {
// Hash record text only, separated by one LF, with no terminal LF. Framing
// newlines are excluded. Count records attempted, including queue drops:
// transport loss must make host verification fail rather than bless a subset.
class DumpCheck {
    struct HashState;
    HashState* state=nullptr;
    unsigned lines=0;
    static constexpr size_t scratch_bytes=1024;
    void update(const char* text,size_t bytes);
public:
    DumpCheck();
    ~DumpCheck();
    DumpCheck(const DumpCheck&)=delete;
    DumpCheck& operator=(const DumpCheck&)=delete;
    void record(const char* text);
    void finish(const char* prefix,char* output,size_t bytes);
};
}
