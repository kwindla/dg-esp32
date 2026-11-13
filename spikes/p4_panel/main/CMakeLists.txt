idf_component_register(SRCS "main.cpp" "dsi_read.cpp" "../../p4_probe/main/console.cpp"
    INCLUDE_DIRS "." "../../p4_probe/main"
    PRIV_REQUIRES esp_lcd esp_hal_lcd esp_mm esp_driver_gpio esp_driver_uart esp_driver_ledc esp_hw_support esp_timer esp_psram esp_ringbuf vfs)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra)
# --wrap rewrites the DBI reference, but does not by itself extract the wrapper
# object from libmain.a. Root it explicitly so archive ordering cannot omit it.
target_link_libraries(${COMPONENT_LIB} INTERFACE
    "-Wl,--undefined=__wrap_mipi_dsi_hal_host_gen_read_dcs_command"
    "-Wl,--wrap=mipi_dsi_hal_host_gen_read_dcs_command")
