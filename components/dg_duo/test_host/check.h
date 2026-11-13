#pragma once
#include <cstdio>
#include <cstdlib>
// Test calls (including side effects) must execute in Release/NDEBUG builds.
#define CHECK(expression) do { if(!(expression)){ \
    std::fprintf(stderr,"%s:%u: CHECK(%s) failed\n",__FILE__,unsigned(__LINE__),#expression); \
    std::abort(); \
} } while(false)
