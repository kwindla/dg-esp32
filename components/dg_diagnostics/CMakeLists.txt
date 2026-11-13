set(console_sources "uart.cpp")
set(console_requires esp_driver_uart)
if(IDF_TARGET STREQUAL "esp32s3")
    list(APPEND console_sources "usb_serial_jtag.cpp")
    list(APPEND console_requires esp_driver_usb_serial_jtag)
endif()
idf_component_register(SRCS "diagnostics.cpp" "console_output.cpp" "console_backend.cpp" "dump_check.cpp" ${console_sources}
    INCLUDE_DIRS "include" REQUIRES mbedtls PRIV_REQUIRES heap esp_timer esp_ringbuf dg_board ${console_requires})
# Pinned IDF 6.1 heap registry: public heap_caps_get_info aggregates regions.
# Isolate this read-only adapter so reserve and main-heap accounting remain exact.
target_include_directories(${COMPONENT_LIB} PRIVATE "$ENV{IDF_PATH}/components/heap")
target_compile_definitions(${COMPONENT_LIB} PRIVATE MULTI_HEAP_FREERTOS)
target_compile_features(${COMPONENT_LIB} PUBLIC cxx_std_17)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra)
target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--wrap=dtls_srtp_handshake")
target_link_libraries(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=esp_log,--wrap=esp_log_va,--wrap=esp_log_write,--wrap=esp_log_writev,--wrap=printf,--wrap=vprintf,--wrap=puts")
