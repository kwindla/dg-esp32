#pragma once
namespace dg::netprobe {
using Write = void (*)(const char*);
// Console-only control. One start can configure two independent destinations.
void command(const char* args, Write write);
}
