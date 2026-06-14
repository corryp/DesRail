# Minimal CMake build for the DesRail simulator.
#
# The reference build is Visual Studio 2022 via ../DESLEARN.sln; this file is
# provided so the simulator can be built on Linux/macOS/MinGW with recent
# GCC (>= 11), Clang (>= 14), or MSVC (>= v143). C++20 coroutines are
# required.
#
# Usage:
#   cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
#   cmake --build build -j
#
# The resulting executable expects to be run from a directory that contains
# `input/` and `output/` subdirectories; see the root CLAUDE.md for details.

cmake_minimum_required(VERSION 3.20)
project(DesRail CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# Coroutines: GCC needs an explicit flag on older versions; Clang and MSVC
# enable them by default under C++20.
if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
    add_compile_options(-fcoroutines)
endif()

if(MSVC)
    add_compile_options(/W3 /permissive-)
else()
    add_compile_options(-Wall -Wextra -Wno-unused-parameter -Wno-sign-compare)
endif()

add_executable(DesRail
    main.cpp
    simulator.cpp
    desrail.cpp
    deadlock_avoidance.cpp
    rail_sim_tools.cpp
    read_csv.cpp
    log_file_util.cpp
    dr_anim.cpp
    desviz_src/desviz_script.cpp
)

target_include_directories(DesRail PRIVATE
    .
    desviz_src
)
