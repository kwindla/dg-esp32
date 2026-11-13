#pragma once
#include <cstdint>
using esp_err_t = int;
constexpr int ESP_OK = 0;
constexpr int ESP_ERR_HTTP_EAGAIN = 0x7007;
constexpr int HTTP_METHOD_POST = 1;
struct FakeHttp;
using esp_http_client_handle_t = FakeHttp*;
struct esp_http_client_config_t { const char* url; int method; int timeout_ms; bool disable_auto_redirect; esp_err_t (*crt_bundle_attach)(void*); };
esp_http_client_handle_t esp_http_client_init(const esp_http_client_config_t*);
esp_err_t esp_http_client_cleanup(esp_http_client_handle_t);
esp_err_t esp_http_client_set_timeout_ms(esp_http_client_handle_t, int);
esp_err_t esp_http_client_set_header(esp_http_client_handle_t, const char*, const char*);
esp_err_t esp_http_client_open(esp_http_client_handle_t, int);
int esp_http_client_write(esp_http_client_handle_t, const char*, int);
int64_t esp_http_client_fetch_headers(esp_http_client_handle_t);
int esp_http_client_get_status_code(esp_http_client_handle_t);
int esp_http_client_read(esp_http_client_handle_t, char*, int);
bool esp_http_client_is_complete_data_received(esp_http_client_handle_t);
