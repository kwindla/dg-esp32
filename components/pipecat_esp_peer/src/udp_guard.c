/* esp_peer's prebuilt dtls_srtp_probe emits a zero-length UDP datagram.
 * aiortc 1.15.0 indexes data[0]. The global wrap changes only size==0 sends;
 * no reliable public peer-socket ownership API exists. No send-path logging. */
#include "lwip/sockets.h"
#include <stdatomic.h>
static atomic_uint suppressed;
unsigned pipecat_empty_udp_count(void) { return atomic_load(&suppressed); }
ssize_t __real_lwip_sendto(int s, const void *data, size_t size, int flags,
                         const struct sockaddr *to, socklen_t tolen);
ssize_t pipecat_profile_sendto(int s, const void *data, size_t size, int flags,
                             const struct sockaddr *to, socklen_t tolen);
ssize_t __wrap_lwip_sendto(int s, const void *data, size_t size, int flags,
                         const struct sockaddr *to, socklen_t tolen) {
    if (size == 0) { atomic_fetch_add(&suppressed, 1); return 0; }
    return pipecat_profile_sendto(s, data, size, flags, to, tolen);
}
