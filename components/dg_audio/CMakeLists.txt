# Select complete implementations; codec sources contain no board branches.
set(stream_sources "src/mic.cpp" "src/speaker.cpp")
if(CONFIG_DG_BOARD_ECHOEAR OR CONFIG_DG_BOARD_P4XC)
    set(codec_sources "src/codec/i2s_duplex.cpp" "src/codec/es8311_es7210.cpp" "src/codec/playback.cpp")
elseif(CONFIG_DG_BOARD_WAVESHARE_146)
    set(codec_sources "src/codec/direct_i2s.cpp")
endif()
idf_component_register(SRCS "src/aec.cpp" ${codec_sources} ${stream_sources} "src/gate.cpp" "src/playback_dsp.cpp" "src/pipeline.cpp" "src/diagnostics.cpp"
    INCLUDE_DIRS "include" LDFRAGMENTS "playback_hot.lf" "aec_coefficients.lf" PRIV_REQUIRES dg_board dg_diagnostics esp_driver_i2s esp_driver_gpio esp_timer pthread)
target_compile_features(${COMPONENT_LIB} PUBLIC cxx_std_17)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -std=gnu++17)
