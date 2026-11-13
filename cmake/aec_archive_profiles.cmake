# Audited processor archives, keyed by target/revision family/IDF major.
# Placement is limited to each target's separately audited coefficient source
# table (P4: decision-2026-11-02-p4-aec-coefficient-placement). Unknown keys fail.
# Mirrors ESP-SR 2.4.7's TARGET_LIB_PATH choice; re-audit this coupling on
# vendor updates. Archive hashes and image checks also bind that selection.
function(dg_aec_archive_profile target less_v3 idf_major output)
    if(NOT "${idf_major}" MATCHES "^[0-9]+$")
        message(FATAL_ERROR "AEC archive selection requires an explicit IDF major")
    endif()
    if("${target}" STREQUAL "esp32p4" AND less_v3)
        set(key "esp32p4_less_v3_idf${idf_major}")
    elseif(less_v3)
        message(FATAL_ERROR "Unsupported AEC target/revision combination: ${target}/less-v3")
    else()
        set(key "${target}_idf${idf_major}")
    endif()

    # Qualified S3 archive/path unchanged.
    set(hash_esp32s3_idf6 "670639c3d136c7e3909aa90bcaac573600b7e98b6850be8fd5484b4cdea96dcc")
    set(directory_esp32s3_idf6 "esp32s3")
    set(board_esp32s3_idf6 "echoear")

    # Step-1b production-equivalent P4 rev<3 archive binding, ESP-SR 2.4.7.
    set(hash_esp32p4_less_v3_idf6 "16269d8abda631b872ef438f1f0a91105f4f6cd876451d44aaa0e65bd26c6100")
    set(directory_esp32p4_less_v3_idf6 "esp32p4_less_v3_idf6")
    set(board_esp32p4_less_v3_idf6 "p4xc")

    if(NOT DEFINED hash_${key})
        message(FATAL_ERROR "Unaudited AEC archive key: ${key}")
    endif()
    set(${output}_KEY "${key}" PARENT_SCOPE)
    set(${output}_DIRECTORY "${directory_${key}}" PARENT_SCOPE)
    set(${output}_HASH "${hash_${key}}" PARENT_SCOPE)
    set(${output}_BOARD "${board_${key}}" PARENT_SCOPE)
endfunction()
