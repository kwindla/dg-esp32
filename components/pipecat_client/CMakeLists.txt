set(upstream "${CMAKE_CURRENT_LIST_DIR}/../../third_party/pipecat-client-cxx")
set(PROJECT_VERSION 1.1.0)
set(PROJECT_VERSION_MAJOR 1)
set(PROJECT_VERSION_MINOR 1)
set(PROJECT_VERSION_PATCH 0)
configure_file("${upstream}/cmake/version.h.in" "${CMAKE_CURRENT_BINARY_DIR}/include/pipecat/version.h" @ONLY)
idf_component_register(
    SRCS "${upstream}/src/audio_buffer.cpp" "${upstream}/src/client.cpp"
         "${upstream}/src/event_loop.cpp" "${upstream}/src/transport.cpp"
         "${upstream}/src/rtvi/about.cpp" "${upstream}/src/rtvi/messages.cpp"
         "src/http_esp.cpp" "src/platform_esp.cpp"
    INCLUDE_DIRS "include" "${upstream}/include" "${CMAKE_CURRENT_BINARY_DIR}/include"
    PRIV_INCLUDE_DIRS "${upstream}/src"
    REQUIRES nlohmann_json
    PRIV_REQUIRES dg_diagnostics esp_http_client esp-tls mbedtls pthread esp_timer)
target_compile_features(${COMPONENT_LIB} PUBLIC cxx_std_17)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra)
set_target_properties(${COMPONENT_LIB} PROPERTIES CXX_STANDARD 17 CXX_STANDARD_REQUIRED ON)
# IDF supplies a newer -std flag through its build properties; override last.
target_compile_options(${COMPONENT_LIB} PRIVATE -std=gnu++17)
