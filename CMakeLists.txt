# ESP-IDF project. The boilerplate below must stay in this order.
cmake_minimum_required(VERSION 3.22)

# Board, variant and overlays are resolved in cmake/dg_select.py, the resolver
# scripts/idf.sh uses, so a plain idf.py build selects the same image as the
# wrapper. An explicit -DSDKCONFIG_DEFAULTS, -DSDKCONFIG, -DIDF_TARGET or
# -DDEPENDENCIES_LOCK still wins: the wrapper passes them, and so does a
# repeatable legacy build.
set(dg_python "${PYTHON}")  # idf.py passes the interpreter it runs under.
if(NOT dg_python)
    set(dg_python python3)
endif()
execute_process(COMMAND "${dg_python}" "${CMAKE_CURRENT_SOURCE_DIR}/cmake/dg_select.py" --cmake
        --project "${CMAKE_CURRENT_SOURCE_DIR}" --binary-dir "${CMAKE_BINARY_DIR}"
    OUTPUT_VARIABLE dg_selection ERROR_VARIABLE dg_selection_error RESULT_VARIABLE dg_selection_failed)
if(dg_selection_failed)
    message(FATAL_ERROR "${dg_selection_error}")
endif()
# The resolver decides the configuration from outside this file, so a change to
# it must reconfigure a build the way a change to this file does.
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/cmake/dg_select.py")
if(DEFINED SDKCONFIG_DEFAULTS)
    set(dg_explicit_defaults "${SDKCONFIG_DEFAULTS}")
endif()
cmake_language(EVAL CODE "${dg_selection}")
if(DEFINED dg_explicit_defaults)
    set(SDKCONFIG_DEFAULTS "${dg_explicit_defaults}")
endif()
if(NOT DEFINED SDKCONFIG)
    set(SDKCONFIG "${dg_selected_config}")
endif()
if(dg_selected_target AND NOT DEFINED IDF_TARGET)
    set(IDF_TARGET "${dg_selected_target}")
endif()
if(dg_selected_lock AND NOT DEFINED DEPENDENCIES_LOCK)
    set(DEPENDENCIES_LOCK "${dg_selected_lock}")
endif()
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# IDF 6.1's manager reads a build property, not merely the CMake cache entry.
if(DEFINED DEPENDENCIES_LOCK)
    idf_build_set_property(DEPENDENCIES_LOCK "${DEPENDENCIES_LOCK}")
endif()
# Only build components that main (transitively) depends on.
idf_build_set_property(MINIMAL_BUILD ON)
project(dg-esp32)
# managed_components/ is one directory for every image, and each target has its
# own lock: EchoEar pins esp_audio_codec 2.6.2, the P4 2.5.0. The component
# manager changes a version in place and keeps the files' old timestamps, so
# ninja saw nothing: an EchoEar image built after a P4 configure linked the
# P4's library, and nothing said so. Only .component_hash is written anew.
# With these as configure inputs, a build re-runs CMake, and with it the
# component manager for this image, whenever a component changed under it.
file(GLOB dg_component_hashes "${CMAKE_SOURCE_DIR}/managed_components/*/.component_hash")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${dg_component_hashes})
execute_process(COMMAND "${PYTHON}" "${CMAKE_CURRENT_SOURCE_DIR}/scripts/check-sdkconfig.py"
    --validate-only "${SDKCONFIG}" RESULT_VARIABLE playback_config_invalid)
if(playback_config_invalid)
    message(FATAL_ERROR "Invalid playback loudness configuration")
endif()

include(cmake/peer_pem_psram.cmake)

include(cmake/display_http_psram.cmake)
include(cmake/display_littlefs_guard.cmake)
include(cmake/echoear_aec_memory.cmake)

include(cmake/p4_display_headroom.cmake)
