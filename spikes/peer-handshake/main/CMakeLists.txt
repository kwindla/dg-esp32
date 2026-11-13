idf_component_register(SRCS "main.c" "signaling.c" "audio.c" "udp_guard.c" INCLUDE_DIRS "."
    PRIV_REQUIRES dg_net esp_wifi nvs_flash esp_netif esp_event esp_http_client esp-tls esp_psram esp_timer pthread mbedtls lwip)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra)
target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--wrap=lwip_sendto" "-Wl,-u,__wrap_lwip_sendto")
