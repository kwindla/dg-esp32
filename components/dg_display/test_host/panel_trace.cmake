find_package(Python3 REQUIRED COMPONENTS Interpreter)
set(DISPLAY_TRACE_SOURCE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/.." CACHE PATH "Production display sources for trace/mutation tests")
set(DISPLAY_TRACE_VENDOR_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../../managed_components" CACHE PATH "Pinned managed LCD drivers")
# Cache the exact fetched sources before CONFIG-conditional dependency switching
# removes an unused managed driver. A fresh checkout needs Waveshare reconfigure.
set(trace_vendor "${CMAKE_CURRENT_BINARY_DIR}/panel-vendor")
foreach(driver esp_lcd_st77916 esp_lcd_spd2010)
    if(EXISTS "${DISPLAY_TRACE_VENDOR_ROOT}/${driver}/${driver}.c")
        file(MAKE_DIRECTORY "${trace_vendor}/${driver}")
        file(COPY "${DISPLAY_TRACE_VENDOR_ROOT}/${driver}/" DESTINATION "${trace_vendor}/${driver}")
    elseif(EXISTS "${DISPLAY_TRACE_VENDOR_ROOT}/espressif__${driver}/${driver}.c")
        file(MAKE_DIRECTORY "${trace_vendor}/${driver}")
        file(COPY "${DISPLAY_TRACE_VENDOR_ROOT}/espressif__${driver}/" DESTINATION "${trace_vendor}/${driver}")
    elseif(NOT EXISTS "${trace_vendor}/${driver}/${driver}.c")
        message(FATAL_ERROR "Panel traces need pinned ${driver}. Run DG_BOARD=waveshare_146 scripts/idf.sh reconfigure first, then scripts/host-tests.sh.")
    endif()
endforeach()
add_library(panel_trace_vendor STATIC
    "${trace_vendor}/esp_lcd_st77916/esp_lcd_st77916.c"
    "${trace_vendor}/esp_lcd_st77916/esp_lcd_st77916_spi.c"
    "${trace_vendor}/esp_lcd_spd2010/esp_lcd_spd2010.c")
target_include_directories(panel_trace_vendor PUBLIC trace_fakes
    "${trace_vendor}/esp_lcd_st77916/include" "${trace_vendor}/esp_lcd_st77916/priv_include"
    "${trace_vendor}/esp_lcd_spd2010/include")
target_compile_definitions(panel_trace_vendor PRIVATE
    ESP_LCD_ST77916_VER_MAJOR=2 ESP_LCD_ST77916_VER_MINOR=0 ESP_LCD_ST77916_VER_PATCH=2
    ESP_LCD_SPD2010_VER_MAJOR=2 ESP_LCD_SPD2010_VER_MINOR=0 ESP_LCD_SPD2010_VER_PATCH=0)
foreach(board st77916_qspi spd2010_qspi)
    set(sources "${DISPLAY_TRACE_SOURCE_ROOT}/backends/${board}/panel_init.cpp")
    if(board STREQUAL st77916_qspi)
        list(APPEND sources "${DISPLAY_TRACE_SOURCE_ROOT}/backends/st77916_qspi/echoear_panel.c")
    endif()
    add_executable(panel_trace_${board} panel_trace.cpp ${sources})
    target_include_directories(panel_trace_${board} PRIVATE trace_fakes ../../dg_board/include)
    target_link_libraries(panel_trace_${board} PRIVATE panel_trace_vendor)
    if(board STREQUAL st77916_qspi)
        target_compile_definitions(panel_trace_${board} PRIVATE CONFIG_DG_BOARD_ECHOEAR=1)
    endif()
    add_test(NAME panel_trace.${board} COMMAND "${Python3_EXECUTABLE}"
        "${CMAKE_CURRENT_SOURCE_DIR}/test_panel_trace.py" "$<TARGET_FILE:panel_trace_${board}>" ${board})
endforeach()
