#pragma once
namespace dg {
// Diagnostic builds only; caller is the serialized console owner.
void heap_attribution_command(const char* args,bool idle,void (*out)(const char*));
}
