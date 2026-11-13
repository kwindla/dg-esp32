set(display_sources "runtime.cpp" "ui/simple/character.cpp" "ui/simple/runtime.cpp"
    "src/present/logical_adapter.cpp" "src/present/presenter.cpp"
    "src/dgi.cpp" "src/scene.cpp" "src/arena.cpp" "src/cache.cpp" "src/clock.cpp"
    "src/command_dispatch.cpp" "src/engine.cpp" "src/flash_store.cpp" "src/json_request.cpp")
if(CONFIG_DG_BOARD_ECHOEAR)
    set(qspi_backend st77916_qspi)
    list(APPEND display_sources "backends/st77916_qspi/echoear_panel.c")
elseif(CONFIG_DG_BOARD_WAVESHARE_146)
    set(qspi_backend spd2010_qspi)
endif()
if(DEFINED qspi_backend)
    list(APPEND display_sources "backends/${qspi_backend}/bringup.cpp" "backends/${qspi_backend}/panel_init.cpp" "backends/qspi/transfer.cpp"
        "backends/qspi/commands.cpp" "backends/qspi/wraps.cpp" "backends/qspi/r3_stress.cpp"
        "backends/qspi/r4_crypto.cpp" "backends/qspi/r5_hash.cpp")
elseif(CONFIG_DG_BOARD_P4XC)
    list(APPEND display_sources "backends/jd9365_dsi/backend.cpp" "backends/jd9365_dsi/framebuffer.cpp"
        "backends/jd9365_dsi/dsi_read.cpp" "backends/jd9365_dsi/runtime.cpp" "backends/null/commands.cpp")
else()
    list(APPEND display_sources "backends/null/null_panel.cpp" "backends/null/backend.cpp" "backends/null/runtime.cpp" "backends/null/commands.cpp")
endif()
idf_component_register(SRCS ${display_sources} INCLUDE_DIRS "include"
    REQUIRES esp_lcd dg_board
    PRIV_REQUIRES dg_diagnostics dg_audio joltwallet__littlefs esp_driver_spi esp_driver_gpio
        esp_http_client esp_partition spi_flash esp_timer heap esp_psram mbedtls pipecat_client)
target_compile_features(${COMPONENT_LIB} PRIVATE cxx_std_17)
target_compile_options(${COMPONENT_LIB} PRIVATE -fstack-usage)
# Component discovery runs before Kconfig; attach managed panel dependencies only
# after registration. Never put CONFIG-dependent components in REQUIRES.
if(CONFIG_DG_BOARD_ECHOEAR)
    idf_component_optional_requires(PRIVATE espressif__esp_lcd_st77916)
elseif(CONFIG_DG_BOARD_WAVESHARE_146)
    idf_component_optional_requires(PRIVATE espressif__esp_lcd_spd2010)
elseif(CONFIG_DG_BOARD_P4XC)
    idf_component_optional_requires(PRIVATE espressif__esp_lcd_jd9365 esp_hal_lcd esp_mm esp_hw_support)
    target_link_libraries(${COMPONENT_LIB} INTERFACE
        "-Wl,--undefined=__wrap_mipi_dsi_hal_host_gen_read_dcs_command"
        "-Wl,--wrap=mipi_dsi_hal_host_gen_read_dcs_command")
endif()
if(DEFINED qspi_backend)
    target_include_directories(${COMPONENT_LIB} PRIVATE "backends/${qspi_backend}")
    # The retired QSPI board used the same S3 observers/guards before this split.
    foreach(symbol spicommon_dma_setup_priv_buffer spi_device_get_trans_result spi_bus_add_device
        spi_device_queue_trans spi_device_polling_transmit esp_flash_read esp_flash_write esp_flash_erase_region
        esp_sha_dma esp_crypto_shared_gdma_start_axi_ahb gdma_new_ahb_channel gdma_connect gdma_start
        esp_aes_process_dma esp_aes_process_dma_gcm)
        target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--wrap=${symbol}")
    endforeach()
endif()
