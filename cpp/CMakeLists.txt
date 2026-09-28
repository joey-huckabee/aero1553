# Aero1553 -- C++ build for MSVC / Visual Studio (ADR-0002)
#
# AUTHORITATIVE ON WINDOWS. cpp/Makefile is authoritative on Linux. Two build
# descriptions rather than one is not a preference: the gcc:4.8 fidelity
# container is Debian 7 and has NO CMAKE AT ALL -- and cannot install one, since
# Debian 7's repositories are archived and apt-get update there exits 100 -- so
# the tier that proves SLES 12 conformance has to be driven by make. Both read
# cpp/sources.txt, so the file list itself cannot drift. ADR-0002 records the
# full reasoning, including why putting a hand-built CMake in that image would
# cost the tier the credibility it exists for.
#
# In Visual Studio 2022: File -> Open -> Folder -> this directory. VS configures
# CMake, wires up IntelliSense and the debugger, and picks up the test target.
# No .sln or .vcxproj is checked in -- hand-maintained project XML is precisely
# the drift this shared source list exists to avoid.
#
# From a command line:
#     cmake -B build -S .
#     cmake --build build --config Debug
#     ctest --test-dir build -C Debug --output-on-failure
#
# 3.15 is the floor: it is what gives target-scoped SYSTEM include directories
# and the generator expressions used below. Every CMake that ships with a
# supported Visual Studio is far newer.
cmake_minimum_required(VERSION 3.15)

project(aero1553
    VERSION 3.0.0
    DESCRIPTION "DDC MIL-STD-1553 MIE binary decoder"
    LANGUAGES CXX
)

# --- Source list, shared with the Makefile ----------------------------------
# file(STRINGS) with REGEX filtering is the CMake counterpart of the sed
# pipeline in cpp/Makefile. Keep the two filters equivalent: a line one build
# treats as a comment and the other treats as a path produces a module that
# compiles on one platform and is missing from the other binary.
file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/sources.txt" MIE_SOURCE_LINES)
set(MIE_LIB_SOURCES "")
foreach(line IN LISTS MIE_SOURCE_LINES)
    string(REGEX REPLACE "#.*$" "" line "${line}")
    string(STRIP "${line}" line)
    if(NOT line STREQUAL "")
        list(APPEND MIE_LIB_SOURCES "${line}")
    endif()
endforeach()

if(MIE_LIB_SOURCES STREQUAL "")
    message(FATAL_ERROR "sources.txt yielded no sources -- the shared source list is not being read")
endif()

# Exactly one platform backend, selected here rather than listed in sources.txt.
# Everything named in that file is compiled unconditionally; these two are
# mutually exclusive and each fails to compile on the other platform by design
# (both carry an #error guard, so a mis-selection is a clear message rather than
# a cascade of missing-symbol noise).
if(WIN32)
    list(APPEND MIE_LIB_SOURCES "src/platform_win32.cpp")
else()
    list(APPEND MIE_LIB_SOURCES "src/platform_posix.cpp")
endif()

# Published into the cache so scripts/assert-sources-agree.sh can ask CMake what
# it actually resolved, rather than re-implementing this parsing loop and then
# comparing the build against a copy of itself.
#
# STRING + FORCE rather than INTERNAL, deliberately: `cmake -LA` -- which is how
# the gate reads it -- lists advanced variables but NOT internal ones, so an
# INTERNAL cache entry here would leave the gate silently reading nothing and
# reporting success.
set(MIE_RESOLVED_SOURCES "${MIE_LIB_SOURCES}" CACHE STRING
    "Library sources as resolved from sources.txt (reporting channel for CI gates)" FORCE)
mark_as_advanced(MIE_RESOLVED_SOURCES)

# --- Library ----------------------------------------------------------------
# Static, and consumed by both the CLI and the test binary. The alternative --
# compiling every source into each target -- doubles build time and lets the two
# drift in flags, which is how a test can pass against code the shipped binary
# does not contain.
add_library(aero1553_lib STATIC ${MIE_LIB_SOURCES})

target_include_directories(aero1553_lib PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>
)

# C++11 is the floor the SLES 12 system compiler sets (ADR-0001). MSVC has no
# /std:c++11 -- its floor is C++14 -- so the Windows build compiles C++11-
# conformant source as C++14, which is valid. The compiler that actually
# enforces the C++11 boundary is GCC 4.8.5 in the fidelity tier, not this one.
#
# That asymmetry is why two C++14 constructs are banned by convention and named
# in CONTRIBUTING.md: aggregate-initialising a class that has a default member
# initialiser, and passing a const_iterator to a container mutator. MSVC accepts
# both and GCC 4.8 rejects both, so they produce "green on Windows, red on
# Linux" -- the failure mode this whole arrangement exists to catch early.
if(MSVC)
    target_compile_features(aero1553_lib PUBLIC cxx_std_14)
else()
    target_compile_features(aero1553_lib PUBLIC cxx_std_11)
endif()

# Warnings-as-errors on both compilers. MSVC's /permissive- makes it STRICTER
# and closer to the standard, which pulls it toward GCC rather than away -- the
# opposite of what the flag name suggests to most readers.
target_compile_options(aero1553_lib PRIVATE
    $<$<CXX_COMPILER_ID:GNU,Clang,AppleClang>:-Wall;-Wextra;-Werror>
    $<$<CXX_COMPILER_ID:MSVC>:/W4;/WX;/permissive-;/utf-8;/Zc:__cplusplus>
)

target_compile_definitions(aero1553_lib PRIVATE
    # 64-bit off_t. MIE recordings routinely exceed 2 GiB, so a silently
    # 32-bit file offset is a live failure rather than a hypothetical one.
    $<$<NOT:$<BOOL:${WIN32}>>:_FILE_OFFSET_BITS=64>
    # Keep <windows.h> from defining min/max macros (which break std::min) and
    # from pulling in the half of the Win32 API this program never touches.
    $<$<BOOL:${WIN32}>:NOMINMAX;WIN32_LEAN_AND_MEAN>
    # The CRT's "secure" variants are Microsoft-only, so adopting them would
    # fork the source. The portable calls are used correctly; the deprecation
    # warning is the thing that is wrong here, not the code.
    $<$<CXX_COMPILER_ID:MSVC>:_CRT_SECURE_NO_WARNINGS>
)

# --- CLI --------------------------------------------------------------------
add_executable(aero1553 src/main.cpp)
if(WIN32)
    # shell32 for CommandLineToArgvW, used to recover the real UTF-16 command
    # line. A platform library, not a third-party one -- L3-CPP-002's
    # no-runtime-dependency rule is about the latter.
    target_link_libraries(aero1553_lib PUBLIC shell32)
endif()

target_link_libraries(aero1553 PRIVATE aero1553_lib)
target_compile_options(aero1553 PRIVATE
    $<$<CXX_COMPILER_ID:GNU,Clang,AppleClang>:-Wall;-Wextra;-Werror>
    $<$<CXX_COMPILER_ID:MSVC>:/W4;/WX;/permissive-;/utf-8;/Zc:__cplusplus>
)
target_compile_definitions(aero1553 PRIVATE
    $<$<CXX_COMPILER_ID:MSVC>:_CRT_SECURE_NO_WARNINGS>
)

# --- Tests ------------------------------------------------------------------
option(MIE_BUILD_TESTS "Build the Catch2 test suite" ON)

if(MIE_BUILD_TESTS)
    enable_testing()

    # Globbed rather than listed, matching the Makefile and for the same reason:
    # a SOURCE file left out of the build fails loudly at link time, whereas a
    # TEST file left out simply never runs. CONFIGURE_DEPENDS makes the build
    # system re-glob when the directory changes, so adding a test file does not
    # require a manual reconfigure.
    file(GLOB MIE_TEST_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/tests/*.cpp")

    add_executable(aero1553_tests ${MIE_TEST_SOURCES})
    target_link_libraries(aero1553_tests PRIVATE aero1553_lib)

    # SYSTEM, so the vendored Catch2 header does not trip /WX or -Werror. This
    # is the CMake counterpart of the Makefile's `-isystem third_party`. Without
    # it the gates would be unusable: a 650 kB third-party header cannot be held
    # to the warning level project code is held to.
    target_include_directories(aero1553_tests SYSTEM PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/third_party"
    )

    target_compile_options(aero1553_tests PRIVATE
        $<$<CXX_COMPILER_ID:GNU,Clang,AppleClang>:-Wall;-Wextra;-Werror>
        $<$<CXX_COMPILER_ID:MSVC>:/W4;/WX;/permissive-;/utf-8;/Zc:__cplusplus;/bigobj>
    )
    target_compile_definitions(aero1553_tests PRIVATE
        $<$<CXX_COMPILER_ID:MSVC>:_CRT_SECURE_NO_WARNINGS>
    )

    # --use-colour no keeps the output readable in CI logs and in the VS Test
    # Explorer pane, both of which render ANSI escapes as literal noise.
    add_test(NAME aero1553_tests COMMAND aero1553_tests --use-colour no)
endif()

# --- Install ----------------------------------------------------------------
include(GNUInstallDirs)
install(TARGETS aero1553 RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
