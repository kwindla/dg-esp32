cmake_minimum_required(VERSION 3.22)
project(audio_logic_tests LANGUAGES C CXX)
if(APPLE)
    # pipeline.cpp and diagnostics.cpp call these weak symbols only when the app defines them; Mach-O
    # needs to be told an undefined weak reference is allowed.
    add_link_options(-Wl,-U,_dg_heap_shadow_sample -Wl,-U,_dg_deferred_log)
endif()
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
include(trace_vendor.cmake)
include(FetchContent)
FetchContent_Declare(googletest
 URL https://github.com/google/googletest/archive/refs/tags/v1.18.0.tar.gz
 URL_HASH SHA256=6e3191c1455468b3fc35a417fb565c1c5071aee1b7e7f85e30cf48a98d37d8b5)
set(INSTALL_GTEST OFF)
set(BUILD_GMOCK OFF)
FetchContent_MakeAvailable(googletest)
add_executable(audio_tests test_gate.cpp test_playback_dsp.cpp test_speaker_clock.cpp test_mic_protection.cpp test_aec_framer.cpp test_aec_probe.cpp ../src/gate.cpp ../src/playback_dsp.cpp)
target_include_directories(audio_tests PRIVATE ../include ../../dg_diagnostics/include)
target_link_libraries(audio_tests PRIVATE GTest::gtest_main)
target_compile_options(audio_tests PRIVATE -Wall -Wextra)
# The same gate tests in the AEC build configuration (decision 131: the AEC path is shown unchanged).
add_executable(gate_aec_config_tests test_gate.cpp ../src/gate.cpp)
target_include_directories(gate_aec_config_tests PRIVATE ../include ../../dg_diagnostics/include)
target_link_libraries(gate_aec_config_tests PRIVATE GTest::gtest_main)
target_compile_options(gate_aec_config_tests PRIVATE -Wall -Wextra)
target_compile_definitions(gate_aec_config_tests PRIVATE CONFIG_DG_ECHOEAR_AEC=1 CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=15 CONFIG_DG_ECHOEAR_AEC_GUARD_MS=400 CONFIG_DG_ECHOEAR_AEC_GUARD_REARM_MS=2000)
include_directories(../../dg_board/include ../src ../include)
enable_testing()
include(GoogleTest)
gtest_discover_tests(audio_tests)
gtest_discover_tests(gate_aec_config_tests TEST_PREFIX aec_config.)

set(PIPELINE_SOURCE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/.." CACHE PATH "Production audio source root")
add_executable(pipeline_i2s_tests test_pipeline_i2s.cpp fakes/fake_i2s.cpp
 ${PIPELINE_SOURCE_ROOT}/src/pipeline.cpp ${PIPELINE_SOURCE_ROOT}/src/speaker.cpp
 ${PIPELINE_SOURCE_ROOT}/src/gate.cpp ${PIPELINE_SOURCE_ROOT}/src/playback_dsp.cpp)
target_include_directories(pipeline_i2s_tests PRIVATE fakes ${PIPELINE_SOURCE_ROOT}/include ../../dg_diagnostics/include)
target_link_libraries(pipeline_i2s_tests PRIVATE GTest::gtest_main)
target_compile_options(pipeline_i2s_tests PRIVATE -Wall -Wextra)
if(PIPELINE_BASELINE)
 target_compile_definitions(pipeline_i2s_tests PRIVATE DG_BASELINE=1)
 target_include_directories(pipeline_i2s_tests BEFORE PRIVATE ${PIPELINE_SOURCE_ROOT}/../dg_diagnostics/include)
endif()

add_executable(echoear_i2s_tests test_echoear_i2s.cpp ../src/codec/i2s_duplex.cpp ../../dg_board/src/echoear/audio_hw.cpp)
target_include_directories(echoear_i2s_tests PRIVATE clock_fakes ../src/codec)
target_compile_options(echoear_i2s_tests PRIVATE -Wall -Wextra)
target_link_libraries(echoear_i2s_tests PRIVATE GTest::gtest_main)
gtest_discover_tests(echoear_i2s_tests)
gtest_discover_tests(pipeline_i2s_tests)

add_executable(pipeline_speech_i2s_tests test_pipeline_speech_i2s.cpp fakes/fake_i2s.cpp
 ${PIPELINE_SOURCE_ROOT}/src/pipeline.cpp ${PIPELINE_SOURCE_ROOT}/src/speaker.cpp
 ${PIPELINE_SOURCE_ROOT}/src/gate.cpp ${PIPELINE_SOURCE_ROOT}/src/playback_dsp.cpp)
target_include_directories(pipeline_speech_i2s_tests PRIVATE fakes ${PIPELINE_SOURCE_ROOT}/include ../../dg_diagnostics/include)
target_compile_definitions(pipeline_speech_i2s_tests PRIVATE CONFIG_DG_PLAYBACK_NORMALIZE=1)
target_link_libraries(pipeline_speech_i2s_tests PRIVATE GTest::gtest_main)
target_compile_options(pipeline_speech_i2s_tests PRIVATE -Wall -Wextra)
if(PIPELINE_BASELINE)
 target_compile_definitions(pipeline_speech_i2s_tests PRIVATE DG_BASELINE=1)
 target_include_directories(pipeline_speech_i2s_tests BEFORE PRIVATE ${PIPELINE_SOURCE_ROOT}/../dg_diagnostics/include)
endif()
gtest_discover_tests(pipeline_speech_i2s_tests)

add_executable(echoear_playback_tests test_echoear_playback.cpp ../src/codec/playback.cpp ../../dg_board/src/echoear/audio_hw.cpp)
target_include_directories(echoear_playback_tests PRIVATE fakes ../src/codec)
target_compile_definitions(echoear_playback_tests PRIVATE CONFIG_DG_BOARD_ECHOEAR=1)
target_compile_options(echoear_playback_tests PRIVATE -include "${CMAKE_CURRENT_SOURCE_DIR}/fakes/playback_codec_fake.h")
target_link_libraries(echoear_playback_tests PRIVATE GTest::gtest_main)
gtest_discover_tests(echoear_playback_tests)

# Compile the actual optional wrapper against a deterministic AFE boundary.
# This exercises lifetime, capture and error propagation without claiming
# cancellation quality from a fake processor.
add_executable(aec_wrapper_tests test_aec_wrapper.cpp ../src/aec.cpp ../../dg_board/src/echoear/audio_hw.cpp)
target_include_directories(aec_wrapper_tests PRIVATE aec_fakes ../include)
target_link_libraries(aec_wrapper_tests PRIVATE GTest::gtest_main)
target_compile_options(aec_wrapper_tests PRIVATE -Wall -Wextra)
gtest_discover_tests(aec_wrapper_tests)

set(MIC_AEC_SOURCE "${CMAKE_CURRENT_SOURCE_DIR}/../src/mic.cpp" CACHE FILEPATH "Actual Mic source for reference-map regression")
add_executable(echoear_mic_tests test_echoear_mic.cpp ${MIC_AEC_SOURCE} ../../dg_board/src/echoear/audio_hw.cpp)
target_include_directories(echoear_mic_tests PRIVATE mic_fakes ../include ../src ../../dg_board/test_host/fakes)
target_link_libraries(echoear_mic_tests PRIVATE GTest::gtest_main)
target_compile_options(echoear_mic_tests PRIVATE -Wall -Wextra)
gtest_discover_tests(echoear_mic_tests)

add_executable(pipeline_aec_probe_tests test_pipeline_aec_probe.cpp fakes/fake_i2s.cpp
 ../src/pipeline.cpp ../src/speaker.cpp ../src/gate.cpp ../src/playback_dsp.cpp)
target_include_directories(pipeline_aec_probe_tests PRIVATE fakes ../include ../../dg_diagnostics/include)
target_compile_definitions(pipeline_aec_probe_tests PRIVATE CONFIG_DG_ECHOEAR_AEC=1)
target_link_libraries(pipeline_aec_probe_tests PRIVATE GTest::gtest_main)
gtest_discover_tests(pipeline_aec_probe_tests)

set(DG_IDF_HEAP_FAILURE_SOURCE "$ENV{IDF_PATH}/components/heap/heap_caps.c" CACHE FILEPATH "Installed IDF heap_caps.c for allocation-failure death tests")
if(EXISTS "${DG_IDF_HEAP_FAILURE_SOURCE}")
 find_package(Python3 REQUIRED COMPONENTS Interpreter)
 set(sdk_failure_test "${CMAKE_CURRENT_BINARY_DIR}/sdk_heap_failure.cpp")
 set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
  "${DG_IDF_HEAP_FAILURE_SOURCE}" "${CMAKE_CURRENT_SOURCE_DIR}/extract_sdk_failure.py")
 execute_process(COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/extract_sdk_failure.py"
  "${DG_IDF_HEAP_FAILURE_SOURCE}" "${sdk_failure_test}" COMMAND_ERROR_IS_FATAL ANY)
 target_sources(aec_wrapper_tests PRIVATE "${sdk_failure_test}")
 target_compile_definitions(aec_wrapper_tests PRIVATE DG_AEC_SDK_ABORT_TEST=1)
endif()

foreach(target pipeline_i2s_tests pipeline_speech_i2s_tests pipeline_aec_probe_tests)
 target_sources(${target} PRIVATE ../src/codec/direct_i2s.cpp ../../dg_board/src/waveshare146/audio_hw.cpp)
 target_compile_definitions(${target} PRIVATE CONFIG_DG_MIC_DMA_DESCRIPTORS=3 CONFIG_DG_MIC_DMA_FRAMES=320)
endforeach()
target_compile_definitions(aec_wrapper_tests PRIVATE CONFIG_DG_SPEAKER_DMA_DESCRIPTORS=4 CONFIG_DG_SPEAKER_DMA_FRAMES=240)

include(trace_tests.cmake)

add_executable(pipeline_echoear_defaults_tests test_pipeline_defaults.cpp pipeline_echoear_fakes.cpp fakes/fake_i2s.cpp
 ../src/pipeline.cpp ../src/speaker.cpp ../src/gate.cpp ../src/playback_dsp.cpp ../../dg_board/src/echoear/audio_hw.cpp)
target_include_directories(pipeline_echoear_defaults_tests PRIVATE fakes ../../dg_diagnostics/include)
target_compile_definitions(pipeline_echoear_defaults_tests PRIVATE CONFIG_DG_BOARD_ECHOEAR=1)
target_link_libraries(pipeline_echoear_defaults_tests PRIVATE GTest::gtest_main)
gtest_discover_tests(pipeline_echoear_defaults_tests)

add_executable(speaker_slot_selection_tests test_speaker_slots.cpp pipeline_echoear_fakes.cpp fakes/fake_i2s.cpp ../src/speaker.cpp)
target_include_directories(speaker_slot_selection_tests PRIVATE fakes ../../dg_diagnostics/include)
target_link_libraries(speaker_slot_selection_tests PRIVATE GTest::gtest_main)
gtest_discover_tests(speaker_slot_selection_tests)

# These focused fakes previously supplied only the feature flag; the shared
# configuration interface requires a complete public AEC setting tuple.
foreach(target aec_wrapper_tests echoear_mic_tests)
 target_compile_definitions(${target} PRIVATE CONFIG_DG_ECHOEAR_AEC_REF_GAIN_DB=15 CONFIG_DG_ECHOEAR_AEC_GUARD_MS=400 CONFIG_DG_ECHOEAR_AEC_GUARD_REARM_MS=2000)
endforeach()

add_executable(null_audio_tests test_null_audio.cpp ../src/codec/null_audio.cpp null_audio_hw.cpp)
target_include_directories(null_audio_tests PRIVATE fakes ../src/codec ../include ../../dg_diagnostics/include)
target_compile_definitions(null_audio_tests PRIVATE CONFIG_DG_MIC_GAIN_PERCENT=100)
target_link_libraries(null_audio_tests PRIVATE GTest::gtest_main)
target_compile_options(null_audio_tests PRIVATE -Wall -Wextra -Werror)
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    # The null backend leaves production members unused; only Clang diagnoses that.
    target_compile_options(null_audio_tests PRIVATE -Wno-unused-private-field)
endif()
gtest_discover_tests(null_audio_tests TEST_PREFIX headless.)
