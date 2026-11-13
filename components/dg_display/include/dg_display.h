#pragma once
#include <cstddef>
namespace dg_display {
// Board initialization and diagnostic compatibility entry points.
// Shared typed requests/results are in dg_display/engine.h.
void census_init(bool external=true, size_t bytes=20480);
void census_provision_empty();
void census_fetch(const char* url);
void census_push(unsigned mhz, unsigned frames);
}
