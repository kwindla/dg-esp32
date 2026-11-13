#include "http.h"
#include "pipecat/esp/http.h"
#include "pipecat/errors.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_timer.h"
#include <algorithm>
#include <cctype>
#include <climits>
#include <memory>
#include <strings.h>
#ifdef ESP_PLATFORM
#include "dg_diagnostics.h"
#endif

namespace pipecat {
namespace {
// Error text reaches the console/session log. A deployment URL's path, query,
// fragment and userinfo may each carry its access secret, so the text names
// scheme://host[:port] and nothing else. An endpoint whose scheme or
// authority is not well formed is not echoed at all.
std::string url_origin(const std::string& url) {
    static const char unnamed[] = "the configured endpoint";
    const auto is = [&](size_t i, int (*kind)(int)) { return kind(static_cast<unsigned char>(url[i])) != 0; };
    const auto scheme = url.find("://");
    if (scheme == std::string::npos || scheme == 0) return unnamed;
    for (size_t i = 0; i < scheme; ++i) if (!is(i, std::isalpha)) return unnamed;
    const auto authority = scheme + 3;
    auto end = url.find_first_of("/?#", authority);
    if (end == std::string::npos) end = url.size();
    const auto at = url.rfind('@', end);
    const auto host = at != std::string::npos && at >= authority ? at + 1 : authority;
    size_t i = host;
    if (i < end && url[i] == '[') {  // IPv6 literal
        for (++i; i < end && (is(i, std::isxdigit) || url[i] == ':' || url[i] == '.'); ++i) {}
        if (i == host + 1 || i == end || url[i] != ']') return unnamed;
        ++i;
    } else {  // name or IPv4 address
        for (; i < end && (is(i, std::isalnum) || url[i] == '-' || url[i] == '.' || url[i] == '_'); ++i) {}
        if (i == host) return unnamed;
    }
    if (i < end) {  // :port
        if (url[i] != ':' || i + 1 == end) return unnamed;
        for (++i; i < end; ++i) if (!is(i, std::isdigit)) return unnamed;
    }
    return url.substr(0, authority) + url.substr(host, end - host);
}
}
HttpResponse http_post(const APIRequest& request, const std::function<bool()>& cancelled) {
    constexpr size_t limit = 64 * 1024;
    const auto duration = request.timeout.count() == 0 ? 30000 : request.timeout.count();
    const int64_t start = esp_timer_get_time();
#ifdef ESP_PLATFORM
    if (request.endpoint.size() >= 6 && request.endpoint.compare(request.endpoint.size()-6, 6, "/start") == 0) dg_start_request();
#endif
    auto fail = [&](const char* reason) { throw StartBotError("Unable to reach " + url_origin(request.endpoint) + ": " + reason); };
    auto remaining_ms = [&]() -> int {
        if (cancelled()) throw PipecatError("start_bot() was cancelled by disconnect()");
        const auto elapsed = (esp_timer_get_time() - start) / 1000;
        if (duration <= elapsed) fail("HTTP deadline expired");
        return static_cast<int>(std::min<int64_t>(duration - elapsed, INT_MAX));
    };
    esp_http_client_config_t cfg{};
    cfg.url = request.endpoint.c_str();
    cfg.method = HTTP_METHOD_POST;
    // A public HTTPS handshake (Funnel, cellular) can take longer than the LAN bound.
    const int open_ms = request.endpoint.rfind("https://", 0) == 0 ? 15000 : 5000;
    cfg.timeout_ms = std::min(remaining_ms(), open_ms);
    cfg.disable_auto_redirect = true;
    // HTTPS (public demo endpoint) verifies against the IDF certificate bundle.
    if (request.endpoint.rfind("https://", 0) == 0) cfg.crt_bundle_attach = esp_crt_bundle_attach;
    auto raw = esp_http_client_init(&cfg);
    if (!raw) fail("HTTP initialization failed");
    const auto cleanup = [](esp_http_client_handle_t h) { esp_http_client_cleanup(h); };
    std::unique_ptr<std::remove_pointer_t<esp_http_client_handle_t>, decltype(cleanup)> client(raw, cleanup);
    auto prepare = [&](int slice = 500) {
        if (esp_http_client_set_timeout_ms(raw, std::min(remaining_ms(), slice)) != ESP_OK)
            fail("setting timeout failed");
    };
    bool content_type = false;
    for (const auto& [name, value] : request.headers) {
        content_type |= strcasecmp(name.c_str(), "Content-Type") == 0;
        if (esp_http_client_set_header(raw, name.c_str(), value.c_str()) != ESP_OK) fail("setting header failed");
    }
    if (!content_type && esp_http_client_set_header(raw, "Content-Type", "application/json") != ESP_OK) fail("setting content type failed");
    const std::string body = request.request_data.is_null() ? "" : request.request_data.dump();
    if (body.size() > INT_MAX) fail("request too large");
    // Synchronous open may postpone cancellation by this bounded call (5 s, 15 s for HTTPS).
    // Never retry/replay a POST here: the request policy owns any retry.
    prepare(open_ms);
    const esp_err_t opened = esp_http_client_open(raw, static_cast<int>(body.size()));
    remaining_ms();
    if (opened != ESP_OK) fail("HTTP open failed");
    for (size_t offset = 0; offset < body.size();) {
        prepare();
        const int n = esp_http_client_write(raw, body.data() + offset, static_cast<int>(body.size() - offset));
        remaining_ms();
        if (n <= 0) fail("HTTP write failed");
        offset += n;
    }
    int64_t length;
    int status = 0;
    do {
        prepare();
        length = esp_http_client_fetch_headers(raw);
        // IDF resets status_code on each fetch_headers call. Preserve a status
        // parsed before a timeout while the remaining headers are arriving.
        const int parsed_status = esp_http_client_get_status_code(raw);
        if (parsed_status > 0) status = parsed_status;
        remaining_ms();
    } while (length == -ESP_ERR_HTTP_EAGAIN);
    if (length < 0) fail("HTTP headers failed");
    if (length > static_cast<int64_t>(limit)) fail("response exceeds 64 KiB");
    HttpResponse response;
    response.status = status;
    if (response.status <= 0) fail("missing HTTP status");
    char buffer[1024];
    for (;;) {
        prepare();
        const int n = esp_http_client_read(raw, buffer, sizeof(buffer));
        remaining_ms();
        if (n == -ESP_ERR_HTTP_EAGAIN) continue;
        if (n < 0) fail("HTTP read failed");
        if (n == 0) {
            if (!esp_http_client_is_complete_data_received(raw)) fail("incomplete HTTP response");
            break;
        }
        if (response.body.size() + n > limit) fail("response exceeds 64 KiB");
        response.body.append(buffer, n);
    }
    return response;
}
}
namespace pipecat::esp {
HttpResponse http_post(const APIRequest& request, const std::function<bool()>& cancelled) {
    auto result = pipecat::http_post(request, cancelled);
    return {result.status, std::move(result.body)};
}
}
