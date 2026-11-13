cmake_minimum_required(VERSION 3.22)
project(esp_peer_logic_tests LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
include(FetchContent)
FetchContent_Declare(googletest
 URL https://github.com/google/googletest/archive/refs/tags/v1.18.0.tar.gz
 URL_HASH SHA256=6e3191c1455468b3fc35a417fb565c1c5071aee1b7e7f85e30cf48a98d37d8b5)
set(INSTALL_GTEST OFF)
set(BUILD_GMOCK OFF)
FetchContent_MakeAvailable(googletest)
add_executable(esp_peer_logic_tests test_interrupt_sequence.cpp test_dtls_record.cpp test_receive_admission.cpp test_inbound_queue.cpp test_send_budget.cpp test_params.cpp test_framer.cpp test_playout.cpp test_rtp_ingress.cpp test_msg_queue.cpp test_timing_histogram.cpp test_owner_totals.cpp test_compact_timing.cpp test_message_carryover.cpp test_wire_contract.cpp
 ../src/params.cpp ../src/framer.cpp ../src/playout.cpp ../src/msg_queue.cpp)
target_include_directories(esp_peer_logic_tests PRIVATE ../src ../../dg_diagnostics/include
 ../../../third_party/pipecat-client-cxx/include ../../nlohmann_json/include)
target_link_libraries(esp_peer_logic_tests PRIVATE GTest::gtest_main)
target_compile_options(esp_peer_logic_tests PRIVATE -Wall -Wextra -UNDEBUG)
enable_testing()
include(GoogleTest)
gtest_discover_tests(esp_peer_logic_tests)
find_library(OPUS_LIBRARY NAMES opus libopus.so.0 REQUIRED)
target_sources(esp_peer_logic_tests PRIVATE test_playout_pcm.cpp test_optional_measures.cpp)
target_link_libraries(esp_peer_logic_tests PRIVATE ${OPUS_LIBRARY})
add_executable(admission_allocation_test test_admission_allocations.cpp)
target_include_directories(admission_allocation_test PRIVATE ../src ../../nlohmann_json/include)
add_test(NAME AdmissionAllocationCensus COMMAND admission_allocation_test)

add_executable(stat_json_tests test_stat_json.cpp ../src/stat_json.cpp)
target_include_directories(stat_json_tests PRIVATE ../include ../../nlohmann_json/include)
target_link_libraries(stat_json_tests PRIVATE GTest::gtest_main)
gtest_discover_tests(stat_json_tests)

# The allocation census also uses assert; keep checks active in Release builds.
target_compile_options(admission_allocation_test PRIVATE -UNDEBUG)
