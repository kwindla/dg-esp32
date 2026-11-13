set(duo_sources)
if(CONFIG_DG_DUO)
    list(APPEND duo_sources "install.cpp" "animator.cpp" "protocol.cpp")
endif()
# Requirements discovery runs before Kconfig. The off variant is interface-only.
idf_component_register(SRCS ${duo_sources} INCLUDE_DIRS "include" "." REQUIRES dg_display)
if(CONFIG_DG_DUO)
    target_compile_features(${COMPONENT_LIB} PRIVATE cxx_std_17)
endif()
