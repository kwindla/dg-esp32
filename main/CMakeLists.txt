set(target_requires)
if(IDF_TARGET STREQUAL "esp32s3")
    list(APPEND target_requires esp_phy esp_driver_usb_serial_jtag)
endif()
idf_component_register(SRCS "main.cpp" "demo_settings.cpp" "demo_boot.cpp" "console.cpp" "app.cpp" "demo_ui.cpp" "performance.cpp" "task_stacks.cpp" "netprobe.cpp" INCLUDE_DIRS "."
    PRIV_REQUIRES nvs_flash esp_hw_support lwip esp_wifi dg_diagnostics dg_audio console esp_timer esp_psram dg_net pipecat_client pipecat_esp_peer pthread esp_peer mbedtls esp_app_format dg_board dg_display dg_duo ${target_requires})
if(CONFIG_DG_CACHE_COUNTERS)
    target_sources(${COMPONENT_LIB} PRIVATE "cache_metrics.cpp")
endif()
if(CONFIG_DG_HEAP_ATTRIBUTION)
    target_sources(${COMPONENT_LIB} PRIVATE "heap_attribution.cpp" "heap_new_callers.cpp" "heap_new_wraps.cpp" "heap_tcp_state.cpp")
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/heap_new_wraps.inc")
    file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/heap_new_wraps.inc" heap_new_wraps REGEX "^HEAP_(NEW|DELETE)\\(")
    foreach(entry IN LISTS heap_new_wraps)
        string(REGEX REPLACE "^HEAP_(NEW|DELETE)\\(([^,]+),.*" "\\2" symbol "${entry}")
        target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--wrap=${symbol}")
    endforeach()
endif()
target_compile_features(${COMPONENT_LIB} PRIVATE cxx_std_17)
if(CONFIG_DG_DUO)
    target_sources(${COMPONENT_LIB} PRIVATE "duo_boot.cpp" "duo_settings.cpp" "duo_ui.cpp" "duo_touch.cpp")
endif()
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -std=gnu++17)
foreach(symbol xTaskCreatePinnedToCore xTaskCreatePinnedToCoreWithCaps xTaskCreateStaticPinnedToCore vTaskDelete)
    target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--wrap=${symbol}")
endforeach()

# Deterministic speech-VAD fixture for digital HIL and optional mic substitution.
target_add_binary_data(${COMPONENT_LIB} "${CMAKE_CURRENT_SOURCE_DIR}/../server/fixtures/prompt_france.wav" BINARY)
target_add_binary_data(${COMPONENT_LIB} "${CMAKE_CURRENT_SOURCE_DIR}/../hil/fixtures/lean_resume.opus" BINARY)

foreach(symbol netconn_new_with_proto_and_callback netconn_bind netconn_delete sys_mbox_trypost)
    target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--wrap=${symbol}")
    # The wrapper is defined in netprobe.cpp inside this archive. Name it undefined
    # up front so the linker extracts that member even when nothing else in main
    # references it (an image without compiled credentials folds run_console away).
    target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--undefined=__wrap_${symbol}")
endforeach()

set_source_files_properties(console.cpp PROPERTIES COMPILE_OPTIONS "-fstack-usage")
