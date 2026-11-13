# This is a placement correction for ONE audited immutable source table,
# not permission to move arbitrary vendor .data. Fail closed on any library
# change, even with the same package version. No managed archive is edited.
if(CONFIG_DG_ECHOEAR_AEC)
    include("${CMAKE_CURRENT_LIST_DIR}/aec_archive_profiles.cmake")
    dg_aec_archive_profile("${IDF_TARGET}" "${CONFIG_ESP32P4_SELECTS_REV_LESS_V3}"
        "${IDF_VERSION_MAJOR}" aec_profile)
    idf_component_get_property(aec_sr_dir espressif__esp-sr COMPONENT_DIR)
    set(aec_processor "${aec_sr_dir}/lib/${aec_profile_DIRECTORY}/libesp_audio_processor.a")
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${aec_processor}")
    file(SHA256 "${aec_processor}" aec_processor_hash)
    set(aec_processor_expected "${aec_profile_HASH}")
    if(NOT aec_processor_hash STREQUAL aec_processor_expected)
        message(FATAL_ERROR "AEC coefficient placement requires re-auditing this ESP-SR archive")
    endif()
    message(STATUS "Audited AEC archive ${aec_profile_KEY}: ${aec_processor_hash}")
    idf_build_get_property(aec_executable EXECUTABLE)
    set(aec_image_check "${CMAKE_CURRENT_SOURCE_DIR}/scripts/check-aec-image.py")
    set_property(TARGET ${aec_executable} APPEND PROPERTY LINK_DEPENDS "${aec_image_check}")
    add_custom_command(TARGET ${aec_executable} POST_BUILD
        COMMAND "${PYTHON}" "${aec_image_check}" --board "${aec_profile_BOARD}"
            --elf "$<TARGET_FILE:${aec_executable}>"
            --archive "${aec_processor}" --ar "${CMAKE_AR}"
            --expected-sha256 "${aec_processor_expected}"
        VERBATIM)
endif()
