#pragma once
#include <cstddef>
namespace pipecat::esp {
// Call in the task that constructs PipecatClient (and its transport).
void configure_threads(int core = 1, int priority = 5, size_t stack = 12288);
}
