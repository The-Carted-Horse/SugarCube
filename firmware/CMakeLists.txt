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

# What this build calls itself when asked whether a release is newer than
# it. The release workflow passes -DGC_VERSION=<version>; a build without
# one is 0.0.0, which every real release outranks, so a board off the bench
# offers an update rather than never seeing one.
#
# Deliberately not "0.0.0-dev": the version grammar both products share
# only knows the pre-release labels alpha, beta, rc and pre, so anything
# else parses as nothing and never compares as newer *or* older.
set(GC_VERSION "0.0.0" CACHE STRING "Version stamped into the firmware")

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
add_compile_definitions(GC_VERSION="${GC_VERSION}")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# One binary name per board: release assets carry the profile in the name,
# and a bin flashed to the wrong board is a black screen with no clue why.
project(glucocube-${GC_BOARD})
