cmake_minimum_required(VERSION 3.22)
project(http_esp_tests LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
include(FetchContent)
FetchContent_Declare(googletest
 URL https://github.com/google/googletest/archive/refs/tags/v1.18.0.tar.gz
 URL_HASH SHA256=6e3191c1455468b3fc35a417fb565c1c5071aee1b7e7f85e30cf48a98d37d8b5)
set(INSTALL_GTEST OFF)
set(BUILD_GMOCK OFF)
FetchContent_MakeAvailable(googletest)
add_executable(http_esp_tests test_http.cpp ../src/http_esp.cpp)
target_include_directories(http_esp_tests PRIVATE shim ../include ../../dg_board/test_host/include)
# Deliberate private upstream access, matching the IDF component.
target_include_directories(http_esp_tests PRIVATE
 ../../../third_party/pipecat-client-cxx/src
 ../../../third_party/pipecat-client-cxx/include ../../nlohmann_json/include)
target_link_libraries(http_esp_tests PRIVATE GTest::gtest_main)
target_compile_options(http_esp_tests PRIVATE -Wall -Wextra)
enable_testing()
include(GoogleTest)
gtest_discover_tests(http_esp_tests)
# Generate the pinned upstream version without requiring another build tree.
file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/../../../third_party/pipecat-client-cxx/CMakeLists.txt" version_line REGEX "^project\\(pipecat VERSION")
string(REGEX MATCH "VERSION ([0-9]+)\\.([0-9]+)\\.([0-9]+)" version_match "${version_line}")
set(PROJECT_VERSION_MAJOR ${CMAKE_MATCH_1})
set(PROJECT_VERSION_MINOR ${CMAKE_MATCH_2})
set(PROJECT_VERSION_PATCH ${CMAKE_MATCH_3})
set(PROJECT_VERSION "${PROJECT_VERSION_MAJOR}.${PROJECT_VERSION_MINOR}.${PROJECT_VERSION_PATCH}")
configure_file(../../../third_party/pipecat-client-cxx/cmake/version.h.in
 ${CMAKE_CURRENT_BINARY_DIR}/generated/pipecat/version.h @ONLY)
add_executable(esp_platform_tests test_platform.cpp
 ../../../third_party/pipecat-client-cxx/src/rtvi/about.cpp
 ../../../third_party/pipecat-client-cxx/src/rtvi/messages.cpp)
target_include_directories(esp_platform_tests PRIVATE shim
 ../../dg_board/test_host/include
 ../../../third_party/pipecat-client-cxx/include
 ${CMAKE_CURRENT_BINARY_DIR}/generated
 ../../nlohmann_json/include)
target_compile_definitions(esp_platform_tests PRIVATE ESP_PLATFORM)
target_compile_options(esp_platform_tests PRIVATE -Wall -Wextra)
target_link_libraries(esp_platform_tests PRIVATE GTest::gtest_main)
gtest_discover_tests(esp_platform_tests)
foreach(board waveshare146 p4xc)
 add_executable(esp_platform_${board}_tests test_platform.cpp
  ../../../third_party/pipecat-client-cxx/src/rtvi/about.cpp
  ../../../third_party/pipecat-client-cxx/src/rtvi/messages.cpp)
 target_include_directories(esp_platform_${board}_tests PRIVATE shim ../../dg_board/test_host/include
  ../../../third_party/pipecat-client-cxx/include ${CMAKE_CURRENT_BINARY_DIR}/generated ../../nlohmann_json/include)
 if(board STREQUAL p4xc)
  target_compile_definitions(esp_platform_${board}_tests PRIVATE DG_TEST_BOARD=3)
 else()
  target_compile_definitions(esp_platform_${board}_tests PRIVATE DG_TEST_BOARD=2)
 endif()
 target_compile_definitions(esp_platform_${board}_tests PRIVATE ESP_PLATFORM)
 target_compile_options(esp_platform_${board}_tests PRIVATE -Wall -Wextra)
 target_link_libraries(esp_platform_${board}_tests PRIVATE GTest::gtest_main)
 gtest_discover_tests(esp_platform_${board}_tests TEST_PREFIX ${board}.)
endforeach()
