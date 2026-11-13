file(WRITE "${OUTPUT}/bad_console.cpp" "#include \"dg_board/console.h\"\nint main() {}\n")
execute_process(COMMAND "${CXX}" -std=c++17 -DTEST_P4=1 -DTEST_USB_ON_P4=1
    -I${SOURCE}/fakes -I${SOURCE}/../../dg_board/include
    -c "${OUTPUT}/bad_console.cpp" -o "${OUTPUT}/bad_console.o"
    RESULT_VARIABLE status OUTPUT_VARIABLE output ERROR_VARIABLE error)
if(status EQUAL 0 OR NOT error MATCHES "USB-JTAG product console requires ESP32-S3")
    message(FATAL_ERROR "Mismatched console/target was not rejected by its compile-time check: ${error}")
endif()
