# P4 rev<3 startup-stack regions are unavailable until the main task runs.
# Enforce the audited pre-scheduler retention budget on every P4 display link.
if(CONFIG_DG_BOARD_P4XC)
    idf_build_get_property(p4_display_executable EXECUTABLE)
    set(p4_display_check "${CMAKE_CURRENT_SOURCE_DIR}/scripts/check_p4_headroom.py")
    set_property(TARGET ${p4_display_executable} APPEND PROPERTY LINK_DEPENDS "${p4_display_check}")
    add_custom_command(TARGET ${p4_display_executable} POST_BUILD
        COMMAND "${PYTHON}" "${p4_display_check}" --elf "$<TARGET_FILE:${p4_display_executable}>"
        VERBATIM)
endif()
