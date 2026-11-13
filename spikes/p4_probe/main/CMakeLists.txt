idf_component_register(SRCS "main.cpp" "console.cpp" "audio_b.cpp" INCLUDE_DIRS "." "../../../components/dg_audio/include"
    PRIV_REQUIRES esp_driver_i2s esp_driver_i2c esp_driver_gpio esp_driver_uart esp_timer esp_psram vfs esp_ringbuf mbedtls)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra)
if(CONFIG_P4_PROBE_OPUS)
    # Compile the exact production wrappers/hooks against the P4 codec pin.
    # Their source files remain unchanged; only this isolated spike links them.
    target_sources(${COMPONENT_LIB} PRIVATE
        "../../../components/pipecat_esp_peer/src/opus.cpp"
        "../../../components/pipecat_esp_peer/src/codec_memory.c")
    target_include_directories(${COMPONENT_LIB} PRIVATE "../../../components/pipecat_esp_peer/src")
endif()
