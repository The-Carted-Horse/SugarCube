# GlucoCube firmware for ESP32-S3 boards with an 800x480 RGB panel.
#
# Build for a board profile (see boards/ and glucocube/contract.py):
#     idf.py -DGC_BOARD=esp32-8048s050 build
#     idf.py -DGC_BOARD=sugarcube-s3   build
#
# The dashboard it draws, the thresholds it applies and the forecast it
# runs all come from glucocube/contract.py, the same file the Raspberry Pi
# image draws from — see components/gc_contract/.

cmake_minimum_required(VERSION 3.16)

set(GC_BOARD "esp32-8048s050" CACHE STRING "Board profile to build for")

if(NOT EXISTS "${CMAKE_CURRENT_LIST_DIR}/boards/${GC_BOARD}/board.h")
    message(FATAL_ERROR
        "Unknown board profile '${GC_BOARD}'. "
        "Profiles live in firmware/boards/ and are listed in "
        "glucocube/contract.py (ESP32_BOARDS).")
endif()

# The board profile reaches every component as a compile definition, so a
# single build of the tree is pinned to one board and cannot mix pin maps.
add_compile_definitions(GC_BOARD_HEADER="boards/${GC_BOARD}/board.h")
add_compile_definitions(GC_BOARD_ID="${GC_BOARD}")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# One binary name per board: release assets carry the profile in the name,
# and a bin flashed to the wrong board is a black screen with no clue why.
project(glucocube-esp32-${GC_BOARD})
