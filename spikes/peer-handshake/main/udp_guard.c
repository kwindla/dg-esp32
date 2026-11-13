/* Isolated interop workaround: esp_peer emits an empty UDP probe before DTLS.
 * aiortc 1.15.0 indexes data[0] and aborts its receive task on that datagram.
 * Empty UDP carries no STUN/DTLS/RTP/SCTP message. Suppress only that probe,
 * retaining the successful zero-byte send return value. Never alter payloads.
 */
#include "lwip/sockets.h"
#include "esp_log.h"
ssize_t __real_lwip_sendto(int s, const void *data, size_t size, int flags,
                         const struct sockaddr *to, socklen_t tolen);
ssize_t __wrap_lwip_sendto(int s, const void *data, size_t size, int flags,
                         const struct sockaddr *to, socklen_t tolen) {
    if (!size) {
        ESP_LOGI("udp-guard", "Suppressed empty UDP probe");
        return 0;
    }
    return __real_lwip_sendto(s,data,size,flags,to,tolen);
}
