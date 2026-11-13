#pragma once
#include "engine.h"
namespace dg_display {
// Console and RTVI adapter share this bounded parser, followed by Engine::post().
// defaults may supply missing metadata for the console; the wire adapter supplies no defaults.
Error parse_request(const char* action,const char* json,size_t length,const Meta* defaults,Request& output) noexcept;
Error parse_server_request(const char* json,size_t length,Request& output,bool& matched,Meta* correlation=nullptr) noexcept;
}
