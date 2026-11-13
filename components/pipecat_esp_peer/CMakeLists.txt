idf_component_register(SRCS "src/transport.cpp" "src/opus.cpp" "src/udp_guard.c" "src/codec_memory.c" "src/codec_benchmark.cpp"
    "src/params.cpp" "src/framer.cpp" "src/playout.cpp" "src/msg_queue.cpp" "src/peer_trace.cpp" "src/stat_json.cpp"
    INCLUDE_DIRS "include" PRIV_INCLUDE_DIRS "src" LDFRAGMENTS "opus_encoder.lf"
    REQUIRES pipecat_client
    PRIV_REQUIRES dg_diagnostics esp_peer esp_audio_codec esp_timer esp_netif lwip dg_net pthread mbedtls)
target_compile_features(${COMPONENT_LIB} PUBLIC cxx_std_17)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra "$<$<COMPILE_LANGUAGE:CXX>:-std=gnu++17>")
target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--wrap=lwip_sendto")
foreach(symbol lwip_recvfrom select lwip_select sys_arch_sem_wait dtls_srtp_decrypt_rtp_packet dtls_srtp_encrypt_rtp_packet dtls_srtp_read dtls_srtp_write mbedtls_ssl_handshake)
    target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--wrap=${symbol}")
endforeach()

if(CONFIG_DG_PLAYBACK_BENCH)
    target_sources(${COMPONENT_LIB} PRIVATE "src/playback_selftest.cpp")
endif()
