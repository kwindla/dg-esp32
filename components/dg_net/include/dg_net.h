#pragma once
#include "esp_err.h"
#include "esp_netif.h"
#ifdef __cplusplus
extern "C" {
#endif
esp_err_t dg_net_init(const char *ssid, const char *password);
esp_err_t dg_net_wifi_connect(unsigned timeout_ms);
const char *dg_net_ip(void);
esp_err_t dg_net_ip_info(esp_netif_ip_info_t *info);
// Called on the IDF event task: notification only, must return immediately.
typedef void (*dg_net_disconnect_cb)(unsigned reason, void *ctx);
void dg_net_set_disconnect_callback(dg_net_disconnect_cb cb, void *ctx);
typedef void (*dg_net_ip_cb)(bool changed, void *ctx);
void dg_net_set_ip_callback(dg_net_ip_cb cb, void *ctx);
#ifdef __cplusplus
}
#endif
#ifdef __cplusplus
#include <string>
namespace dg_net {
inline std::string http_url_join(std::string base, const std::string& path) {
    while (!base.empty() && base.back() == '/') base.pop_back();
    const auto first = path.find_first_not_of('/');
    return base + "/" + (first == std::string::npos ? "" : path.substr(first));
}
}
#endif
