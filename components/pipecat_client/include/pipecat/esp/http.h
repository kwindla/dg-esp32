#pragma once
#include "pipecat/client.h"
#include <functional>
namespace pipecat::esp {
// Public value type: no private upstream headers leak to consumers.
struct HttpResponse { long status; std::string body; };
// ESP backend: timeout zero means 30 seconds; response limit is 64 KiB.
HttpResponse http_post(const APIRequest&, const std::function<bool()>& cancelled);
}
