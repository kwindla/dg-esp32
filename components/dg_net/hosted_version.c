#include "esp_hosted.h"
#include "esp_log.h"
#include "sdkconfig.h"
#if CONFIG_PM_SLEEP_CLK_ICG_ENABLE
#error "P4-XC hosted startup requires PM_SLEEP_CLK_ICG_ENABLE=n (facts A)"
#endif
void dg_net_hosted_version(void) {
    esp_hosted_coprocessor_fwver_t version={0};
    int rc=esp_hosted_get_coprocessor_fwversion(&version);
    // The factory slave returns -1. Zero-filled fields are not its version.
    if(rc) ESP_LOGW("dg-net","HOSTED FW unavailable rpc_rc=%d",rc);
    else ESP_LOGI("dg-net","HOSTED FW rpc_rc=0 version=%lu.%lu.%lu revision=%ld prerelease=%ld build=%ld",
        (unsigned long)version.major1,(unsigned long)version.minor1,(unsigned long)version.patch1,
        (long)version.revision,(long)version.prerelease,(long)version.build);
}
