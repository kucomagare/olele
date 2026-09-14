# Copyright (C) 2023 Advanced Micro Devices, Inc.  All rights reserved.
# SPDX-License-Identifier: MIT
cmake_minimum_required(VERSION 3.14.7)

set(BUILD_CONFIG_DIR ${CMAKE_CURRENT_SOURCE_DIR}/build_config)

include(${BUILD_CONFIG_DIR}/Lwip_tcp_perf_clientExample.cmake)
set(APP_NAME lwip_tcp_perf_client)
project(${APP_NAME})

find_package(common)
include(${BUILD_CONFIG_DIR}/lwip_tcp_perf_client.cmake NO_POLICY_SCOPE)
include(${BUILD_CONFIG_DIR}/UserConfig.cmake)
enable_language(C ASM)

# ---- Sources: add new .c files here --------------------------------------
# Order is link order; keep appending at the end rather than re-sorting.
collect (PROJECT_LIB_SOURCES src/net/lwip_comm_client_raw.c)
collect (PROJECT_LIB_SOURCES src/util/comm_log.c)
collect (PROJECT_LIB_SOURCES src/util/mono_clock.c)
collect (PROJECT_LIB_SOURCES src/net/rx_ring.c)
collect (PROJECT_LIB_SOURCES src/processing/axi_processing.c)
collect (PROJECT_LIB_SOURCES src/processing/dma_stream.c)
collect (PROJECT_LIB_SOURCES src/main.c)
collect (PROJECT_LIB_SOURCES platform/platform.c)
collector_list (_sources PROJECT_LIB_SOURCES)

# Headers are included by bare name ("rx_ring.h"), so every source folder
# is on the include path. generated/ holds packet_format.h from build_app.sh.
set(APP_INCLUDE_DIRS
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/src/net
    ${CMAKE_CURRENT_SOURCE_DIR}/src/processing
    ${CMAKE_CURRENT_SOURCE_DIR}/src/util
    ${CMAKE_CURRENT_SOURCE_DIR}/platform
    ${CMAKE_BINARY_DIR}/generated)

# ---- Libraries -------------------------------------------------------------
collect(PROJECT_LIB_DEPS xilstandalone)
collect(PROJECT_LIB_DEPS xil)
collect(PROJECT_LIB_DEPS xiltimer;lwip213)
collect(PROJECT_LIB_DEPS lwip213)
collect(PROJECT_LIB_DEPS gcc)
collect(PROJECT_LIB_DEPS c)
collector_list (_deps PROJECT_LIB_DEPS)
list (APPEND _deps ${USER_LINK_LIBRARIES})

if(CMAKE_EXPORT_COMPILE_COMMANDS)
    set(CMAKE_CXX_STANDARD_INCLUDE_DIRECTORIES ${CMAKE_CXX_IMPLICIT_INCLUDE_DIRECTORIES})
    set(CMAKE_C_STANDARD_INCLUDE_DIRECTORIES ${CMAKE_C_IMPLICIT_INCLUDE_DIRECTORIES})
endif()

# ---- Target ----------------------------------------------------------------
# Don't restore the vendor linker_gen() call: it writes a default lscript.ld
# into this directory whenever there isn't one here, i.e. on every build now.
set(LINKER_SCRIPT ${BUILD_CONFIG_DIR}/lscript.ld)

string(APPEND CMAKE_C_FLAGS ${USER_COMPILE_OPTIONS})
string(APPEND CMAKE_CXX_FLAGS ${USER_COMPILE_OPTIONS})
string(APPEND CMAKE_C_LINK_FLAGS ${USER_LINK_OPTIONS})
string(APPEND CMAKE_CXX_LINK_FLAGS ${USER_LINK_OPTIONS})
set_source_files_properties(${_sources} OBJECT_DEPENDS "${CMAKE_LIBRARY_PATH}/*.a")
add_executable(${APP_NAME}.elf ${_sources})
set_target_properties(${APP_NAME}.elf PROPERTIES LINK_DEPENDS ${LINKER_SCRIPT})
target_link_libraries(${APP_NAME}.elf -Wl,-T -Wl,\"${LINKER_SCRIPT}\" -L\"${CMAKE_SOURCE_DIR}/\" -L\"${CMAKE_LIBRARY_PATH}/\" -L\"${USER_LINK_DIRECTORIES}/\" -Wl,--start-group,-l${_deps} -Wl,--end-group)
target_compile_definitions(${APP_NAME}.elf PUBLIC ${USER_COMPILE_DEFINITIONS})
target_include_directories(${APP_NAME}.elf BEFORE PRIVATE ${APP_INCLUDE_DIRS})
target_include_directories(${APP_NAME}.elf PUBLIC "${CMAKE_BINARY_DIR}/include")
print_elf_size(CMAKE_SIZE ${APP_NAME})
if (${NON_YOCTO})
   set (INCLUDE_DIRS
       "${CMAKE_INCLUDE_PATH}/include/"
       "${CMAKE_BINARY_DIR}/include")
   target_include_directories(${APP_NAME}.elf PUBLIC ${INCLUDE_DIRS} ${USER_INCLUDE_DIRECTORIES})
endif()
