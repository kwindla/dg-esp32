idf_component_register(SRCS "main.cpp" "../../p4_probe/main/console.cpp" INCLUDE_DIRS "." "../../p4_probe/main"
    PRIV_REQUIRES esp_wifi esp_event esp_netif nvs_flash esp_timer esp_psram lwip
        esp_driver_uart vfs esp_ringbuf mbedtls esp_hosted)
