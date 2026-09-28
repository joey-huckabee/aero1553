# Aero1553 -- C++ build (ADR-0002)
# Target toolchain: GCC 4.8.5 on SLES 12 SP5. Keep everything C++11-clean.
#
# This Makefile is AUTHORITATIVE on Linux. CMakeLists.txt exists alongside it
# for the MSVC/Visual Studio build and is authoritative there. The reason for
# two build descriptions rather than one is not preference: the gcc:4.8 fidelity
# container is Debian 7 and has NO CMAKE AT ALL -- and cannot install one, since
# Debian 7's repositories are archived and apt-get update there exits 100 -- so
# the tier that proves SLES 12 conformance has to be driven by make. Both read
# cpp/sources.txt so the file list cannot drift. ADR-0002 records the full
# reasoning.

CXX      ?= g++
CPPFLAGS += -Iinclude -isystem third_party -D_FILE_OFFSET_BITS=64

# --- Memory-safety instrumentation (modern toolchains; CI jobs) -------------
# Usage: make check SANITIZE=1     (AddressSanitizer + UBSan + LeakSanitizer)
#        make check-valgrind       (Valgrind memcheck, error on any finding)
#
# GCC 4.8 has only partial ASan and no LSan, so the sanitizer tiers run on the
# modern compiler. The code under test is identical because the source is
# strictly C++11-conformant and compiled from one body -- which is the argument
# ADR-0001 rests on, and the reason the 4.8 tier runs the FULL suite rather than
# a compile check.
#
# This block must precede BUILD_DIR, which keys on TIER: make expands target
# patterns as it parses, so a later assignment would not reach the rules below.
SANITIZE ?= 0
COVERAGE ?= 0
ifeq ($(COVERAGE),1)
# Coverage instrumentation. -O0 because optimisation reorders and merges basic
# blocks, which makes branch counts describe the OPTIMISED control flow rather
# than the source a reader is looking at -- the numbers stop meaning what the
# report says they mean.
#
# `--coverage` is in CXXFLAGS rather than split across CXXFLAGS/LDFLAGS because
# the link rules below pass CXXFLAGS to the link step, and gcov needs the flag
# in both places (it implies -lgcov when linking).
CXXFLAGS ?= -std=c++11 -Wall -Wextra -Werror -O0 -g --coverage
TIER := coverage
else ifeq ($(SANITIZE),1)
# -Wno-maybe-uninitialized is disabled FOR THIS TIER ONLY.
#
# Under -O1 plus instrumentation the diagnostic false-positives deep inside
# libstdc++: GCC 14 reports it against <regex> internals (_NFA::_M_insert_alt,
# _M_insert_repeat) reached through Catch2's tag parser -- code this project
# neither wrote nor includes directly. There is a certain irony in it, since
# ADR-0001 bans <regex> from project code outright; the ban cannot reach into
# the test framework.
#
# What makes this safe is NOT that the sanitizers cover the same ground -- they
# do not. ASan finds memory errors and UBSan finds undefined behaviour; reading
# an uninitialized value is MemorySanitizer's job, and MSan is not run here (it
# cannot coexist with ASan, and it needs an instrumented libstdc++ to avoid
# drowning in false positives of its own).
#
# What makes it safe is that the warning is only suppressed on THIS TIER. The
# default -O2 tier and the GCC 4.8.5 fidelity tier both keep
# -Wmaybe-uninitialized at -Werror over the same sources, so a genuine
# uninitialized read in project code still fails the build -- twice. The only
# thing lost is a diagnostic that, at -O1 with instrumentation, fires on
# libstdc++ rather than on us.
#
# Found by CI, not locally: the sanitizer tier runs g++-14 there and g++ 11 on
# the WSL2 host, and GCC 11 does not emit this. `make verify-ci` now runs this
# tier in the CI image for exactly that reason.
CXXFLAGS ?= -std=c++11 -Wall -Wextra -Werror -O1 -g \
            -Wno-maybe-uninitialized \
            -fsanitize=address,undefined -fno-omit-frame-pointer \
            -fno-sanitize-recover=all
TIER := asan
else
CXXFLAGS ?= -std=c++11 -Wall -Wextra -Werror -O2 -g
TIER := default
endif

# Objects are keyed on the toolchain that produced them so the tiers cannot
# clobber one another. Running the WSL build and then the gcc:4.8 container
# build over a shared directory leaves a glibc-2.34 binary in place for the
# container to execute, which fails with a wall of "GLIBC_2.xx not found" and
# reads as a code fault rather than the stale artifact it is.
#
# Override per invocation when needed: make BUILD_DIR=/somewhere/else
TOOLCHAIN ?= $(shell $(CXX) -dumpmachine 2>/dev/null)-$(shell $(CXX) -dumpversion 2>/dev/null)-$(TIER)
BUILD_DIR ?= build/$(TOOLCHAIN)

# --- Sources ----------------------------------------------------------------
# The portable library sources come from sources.txt, shared with CMake. Comment
# and blank lines are stripped here rather than in the file so the file stays
# readable. $(file <) would be tidier but needs GNU make 4.2; SLES 12 ships 4.0.
#
# The comment character is written as \x23 rather than literally. make strips
# comments before expanding a line, INCLUDING inside $(shell ...), so a literal
# one here eats the closing paren and the file dies with "unterminated call to
# function shell". GNU make 4.3 happens to tolerate it; make 3.81 -- which is
# what the gcc:4.8 fidelity container ships -- does not, so this failed only in
# the tier that matters. GNU sed has understood \xHH since 4.0.
LIB_SRC  := $(shell sed -e 's/\x23.*//' -e '/^[[:space:]]*$$/d' sources.txt)

# Exactly one platform backend, chosen by the host. Deliberately not in
# sources.txt: everything named there is compiled unconditionally, and these two
# are mutually exclusive.
LIB_SRC  += src/platform_posix.cpp

LIB_OBJ  := $(patsubst src/%.cpp,$(BUILD_DIR)/src/%.o,$(LIB_SRC))

# Tests are globbed rather than listed. This is the opposite of the sources.txt
# decision on purpose: a source file omitted from the build fails loudly at
# link time, whereas a TEST file omitted from the build simply does not run --
# it fails silently, and a silently-skipped test is worse than a wildcard.
TEST_SRC := $(sort $(wildcard tests/*.cpp))
TEST_OBJ := $(patsubst tests/%.cpp,$(BUILD_DIR)/tests/%.o,$(TEST_SRC))

TEST_BIN    := $(BUILD_DIR)/aero1553_tests
DECODER_BIN := $(BUILD_DIR)/aero1553
DECODER_OBJ := $(BUILD_DIR)/src/main.o

# Header dependency tracking: without it, editing a header rebuilds nothing and
# the next test run silently exercises stale objects.
DEPFLAGS := -MMD -MP

.PHONY: all check check-fuzz clean clean-all toolchain versions check-valgrind \
        check-gcc48 verify-ci print-test-bin print-decoder-bin \
        print-lib-sources decoder tidy tidy-db-check cppcheck format format-check \
        coverage coverage-report

all: $(TEST_BIN) $(DECODER_BIN)

decoder: $(DECODER_BIN)

$(BUILD_DIR)/src/%.o: src/%.cpp
	@mkdir -p $(dir $@)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(DEPFLAGS) -c $< -o $@

$(BUILD_DIR)/tests/%.o: tests/%.cpp
	@mkdir -p $(dir $@)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(DEPFLAGS) -c $< -o $@

$(TEST_BIN): $(LIB_OBJ) $(TEST_OBJ)
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) $^ -o $@

$(DECODER_BIN): $(LIB_OBJ) $(DECODER_OBJ)
	@mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS) $^ -o $@

-include $(LIB_OBJ:.o=.d) $(TEST_OBJ:.o=.d) $(DECODER_OBJ:.o=.d)

# --use-colour no keeps CI logs readable; Catch2 otherwise emits ANSI escapes
# that a log viewer renders as noise around every assertion.
check: $(TEST_BIN)
	$(TEST_BIN) --use-colour no

# The [fuzz] cases only (L1-ROB-001), for the nightly burn-in.
#
# `check` stays the single unparameterised command a developer runs, and the
# fuzz cases still ride along in it at their default iteration count. This
# target exists because the burn-in raises MIE_FUZZ_ITERATIONS by two orders of
# magnitude, and running the WHOLE suite at that point means the job's wall time
# is mostly a cold build plus ~480 unrelated cases -- so it could not be
# compared against the Rust and Python burn-ins, which run only their harnesses.
# Catch2 reports no per-case duration without `-d yes`, so there was no way to
# recover the fuzz time from the combined run either.
check-fuzz: $(TEST_BIN)
	$(TEST_BIN) --use-colour no "[fuzz]"

clean:
	rm -rf $(BUILD_DIR)

clean-all:
	rm -rf build

toolchain:
	@echo $(BUILD_DIR)

# Printed at the head of every CI job so a toolchain change shows up as a
# visible diff in the log rather than as an unexplained warning somewhere else.
versions:
	@echo "CXX       : $(CXX)"
	@$(CXX) --version | head -1
	@echo "TOOLCHAIN : $(TOOLCHAIN)"
	@echo "BUILD_DIR : $(BUILD_DIR)"
	@echo "SOURCES   : $(words $(LIB_SRC)) library, $(words $(TEST_SRC)) test"

print-test-bin:
	@echo $(TEST_BIN)

# The Makefile's own view of the library source set, for
# scripts/assert-sources-agree.sh. That gate asks the build rather than
# re-implementing the sources.txt parsing, so it compares against the build
# itself instead of against a copy that could drift alongside it.
print-lib-sources:
	@echo $(LIB_SRC)

# Used by tests/conformance/run.py so the harness never re-derives BUILD_DIR --
# one place computes the toolchain-keyed path, and every caller asks it.
print-decoder-bin:
	@echo $(DECODER_BIN)

check-valgrind: $(TEST_BIN)
	valgrind --tool=memcheck --leak-check=full --show-leak-kinds=all \
	  --errors-for-leak-kinds=all --error-exitcode=1 \
	  $(TEST_BIN) --use-colour no

# --- Deployment-fidelity tier (GCC 4.8.5) -----------------------------------
# A MIRROR of docker.io/library/gcc:4.8, not that image directly. The upstream
# tag was published in 2016 with a Docker manifest v2 *schema 1*, which modern
# Docker disables by default -- the job dies at the pull with exit 125 before
# compiling anything, while the same tier passes locally because podman still
# accepts schema 1. The mirror is the identical image republished with a v2s2
# manifest. Shared with background-file-mover, which published it.
GCC48_IMAGE ?= ghcr.io/joey-huckabee/gcc-4.8:4.8.5
CI_ENGINE   ?= $(shell command -v podman >/dev/null 2>&1 && echo podman || echo docker)
CI_JOBS     ?= $(shell nproc 2>/dev/null || echo 4)

# Runs the fidelity tier the way CI runs it: one target, one image, both
# callers. Three separately maintained copies of this invocation -- in a
# workflow, in CONTRIBUTING.md, and in a developer's shell history -- is exactly
# how CI and local came to pull different images without either noticing.
#
# NOTE FOR WSL2: this must run from a Linux-native path. Rootless podman cannot
# bind-mount a /mnt/c DrvFs path; the container starts, the mount is silently
# empty, and g++ reports "no input files" as though the source had vanished.
check-gcc48:
	$(CI_ENGINE) run --rm -v "$(CURDIR)":/cpp -w /cpp $(GCC48_IMAGE) \
	    sh -c "make clean && make -j$(CI_JOBS) check"

# --- Static analysis and formatting -----------------------------------------
TIDY   ?= clang-tidy
FORMAT ?= clang-format

# Only project sources: vendored headers arrive via -isystem precisely so they
# do not trip the gates, and pointing an analyzer at them would undo that.
#
# clang-tidy sees only what this host compiles. It cannot analyse
# platform_win32.cpp on Linux -- there is no <windows.h> to parse -- so that
# file is covered by the MSVC tier's /W4 /WX instead.
TIDY_SRC := $(LIB_SRC) src/main.cpp $(TEST_SRC)

# clang-format has no such excuse: it never compiles anything, so it can and
# must cover BOTH backends. Leaving the inactive one out means the Windows
# backend is never format-checked on the Linux tiers and never format-checked on
# the Windows tier either (which runs no clang-format), so it drifts silently
# until someone opens it.
FORMAT_SRC := $(sort $(TIDY_SRC) src/platform_posix.cpp src/platform_win32.cpp) \
              $(wildcard include/mie/*.hpp) $(wildcard tests/*.hpp)

# --warnings-as-errors='*' lives HERE, not in .clang-tidy's WarningsAsErrors.
#
# clang-tidy exits 0 when it emits warnings unless told otherwise, so a gate
# without this flag prints findings and reports success -- which is worse than
# no gate, because the log looks like it was checked. The enabled/disabled check
# list stays in .clang-tidy where the reasoning lives; the severity is a property
# of running it as a gate.

# ---------------------------------------------------------------------------
# Coverage (gcov via gcovr). Gate: 90% lines AND 90% branches.
# ---------------------------------------------------------------------------
# Branches, not "regions": gcov has no region counter. Branch coverage is the
# nearest equivalent to what `cargo llvm-cov` calls regions, and it is the
# stricter of the two numbers here, so gating on it is the meaningful check.
#
# Two exclusions, both deliberate:
#
#   src/main.cpp          -- the executable entry point and nothing else. Its
#                            body is three calls; the CLI it delegates to is
#                            tested in-process. The Rust gate ignores its own
#                            `bin/aero1553.rs` for the same reason, so this
#                            keeps the two numbers comparable.
#   src/platform_win32.cpp -- not compiled on this platform, so it produces no
#                            .gcno and gcov never sees it. Named here anyway so
#                            the exclusion is a stated fact rather than an
#                            accident of which host ran the build. It is covered
#                            by the MSVC tier, which has no coverage measurement
#                            -- a real gap, and one this gate does not close.
#
# Runs the FULL suite: coverage of a subset would be a number about the subset.
# FLOORS, not targets. Both sit at what the suite measures today so nothing can
# regress, and both are meant to be raised as tests land -- the approach
# the delivery plan argued for: a number chosen after measuring rather than
# before.
#
# Lines and branches are far apart (91% vs 76%) and that gap is not all test
# quality. gcov counts every conditional the compiler emits, including ones the
# source does not spell out, so gcov branch coverage runs systematically lower
# than the llvm-cov REGION coverage the Rust gate uses. 90% branches here is a
# materially harder bar than 90% regions there, which is why the two numbers in
# this repository are not equal and should not be forced to match.
#
# GCOV BRANCH COVERAGE IS NOT PORTABLE ACROSS COMPILER VERSIONS. The same suite
# over the same sources measures 76.5% branches on g++ 11.4 (Ubuntu 22.04, the
# documented WSL2 host) and 81.5% on CI's ubuntu-24.04 g++ -- a 5pp spread from
# nothing but the compiler, because branch counts follow the conditionals the
# compiler emits, not the ones the source spells out. Lines barely move (91.1%
# vs 90.9%).
#
# So this single threshold is set to hold on the OLDEST compiler in use, not the
# newest. 76 is the floor that passes everywhere; CI has ~5pp of slack against
# it. The alternative -- pinning CI to its own higher number -- was rejected
# because it makes a local `make coverage` pass while CI fails, which is the
# exact local/CI divergence that has cost this project several red builds.
#
# The ratchet target is therefore CI's number, currently 81.5%.
COVERAGE_MIN_LINE   ?= 90
COVERAGE_MIN_BRANCH ?= 76
# gcov must match the compiler that wrote the .gcno files; a mismatched
# pair fails with "version 'B14*', prefer 'A11*'" and reports nothing.
GCOV  ?= $(if $(filter g++%,$(notdir $(CXX))),$(subst g++,gcov,$(notdir $(CXX))),gcov)
GCOVR ?= gcovr

coverage:
	$(MAKE) --no-print-directory clean COVERAGE=1
	$(MAKE) --no-print-directory check COVERAGE=1
	$(GCOVR) --root . \
	  --gcov-executable "$(GCOV)" \
	  --filter 'src/' \
	  --exclude 'src/main\.cpp' \
	  --exclude 'src/platform_win32\.cpp' \
	  --exclude-unreachable-branches \
	  --exclude-throw-branches \
	  --print-summary \
	  --fail-under-line $(COVERAGE_MIN_LINE) \
	  --fail-under-branch $(COVERAGE_MIN_BRANCH)

# Same measurement, no gate -- for looking at where the misses are.
coverage-report:
	$(MAKE) --no-print-directory clean COVERAGE=1
	$(MAKE) --no-print-directory check COVERAGE=1
	$(GCOVR) --root . --gcov-executable "$(GCOV)" --filter 'src/' \
	  --exclude 'src/main\.cpp' --exclude 'src/platform_win32\.cpp' \
	  --exclude-unreachable-branches --exclude-throw-branches --branches

# A translation unit missing from compile_commands.json makes clang-tidy print
# "Skipping <file>. Compile command not found." and exit 0. Every file missing
# makes it analyse NOTHING and exit 0 -- a gate whose log reads as a clean run.
#
# That is the same failure --warnings-as-errors is there to prevent, one level
# up, and it is not hypothetical: an incremental `bear -- make all` captures no
# command for a target that was already built, so a local `make tidy` skipped
# every test file, reported clean, and CI (which always builds from scratch)
# then failed on one of them. The version-skew banner below says a local run can
# be WEAKER than CI; it must not be able to be EMPTY.
#
# Checked by grep rather than jq: this has to work on the SLES 12 host and in
# the fidelity container, neither of which is required to have a JSON parser.
#
# THIS IS PERMANENT, not scaffolding. A CMake+Ninja build would emit
# compile_commands.json at configure time and make the whole failure mode
# impossible, and that migration was considered and declined: the gcc:4.8
# fidelity container has no CMake, cannot install one (Debian 7's repositories
# are archived, and `apt-get update` there exits 100), and putting a hand-built
# CMake inside the tier whose job is faithful reproduction of the target defeats
# the tier. So bear stays, and so does this.
#
# The search walks UPWARD from here, which is not tidiness -- it is what
# clang-tidy itself does when `-p` names a directory holding no database, and
# this check is worthless unless it looks in the same places. CI proves the
# point: it runs `bear -- make -C cpp all` from the REPOSITORY ROOT, so the
# database lands beside the repo rather than inside cpp/, and clang-tidy has
# been finding it by that upward walk all along. A guard that only looked in
# cpp/ failed a CI job whose tidy run was, in fact, complete.
tidy-db-check:
	@db=""; \
	 dir="$$(pwd)"; \
	 if [ -f "$(BUILD_DIR)/compile_commands.json" ]; then \
	   db="$(BUILD_DIR)/compile_commands.json"; \
	 else \
	   while [ -n "$$dir" ] && [ "$$dir" != "/" ]; do \
	     if [ -f "$$dir/compile_commands.json" ]; then db="$$dir/compile_commands.json"; break; fi; \
	     dir="$$(dirname "$$dir")"; \
	   done; \
	   if [ -z "$$db" ] && [ -f "/compile_commands.json" ]; then db="/compile_commands.json"; fi; \
	 fi; \
	 if [ -z "$$db" ]; then \
	   printf '\n*** make tidy: no compile_commands.json found here or above.\n'; \
	   printf '*** clang-tidy would skip every file and exit 0 -- a gate that\n'; \
	   printf '*** checks nothing and reports success. Generate the database first:\n'; \
	   printf '***   bear -- make all\n\n'; \
	   exit 1; \
	 fi; \
	 missing=""; \
	 for f in $(TIDY_SRC); do \
	   grep -q "/$$f\"" "$$db" || missing="$$missing $$f"; \
	 done; \
	 if [ -n "$$missing" ]; then \
	   printf '\n*** make tidy: %s has no compile command for:\n' "$$db"; \
	   for f in $$missing; do printf '***   %s\n' "$$f"; done; \
	   printf '*** clang-tidy would SKIP those and still exit 0. The database is\n'; \
	   printf '*** stale -- an incremental build captures nothing for a target that\n'; \
	   printf '*** was already up to date. Regenerate it from a clean tree:\n'; \
	   printf '***   make clean && bear -- make all\n\n'; \
	   exit 1; \
	 fi

tidy: tidy-db-check
	@actual=$$($(TIDY) --version 2>/dev/null | sed -n 's/.*LLVM version \([0-9][0-9]*\).*/\1/p' | head -1); \
	 if [ -n "$$actual" ] && [ "$$actual" != "$(LLVM_VERSION)" ]; then \
	   printf '\n*** clang-tidy VERSION SKEW: %s is LLVM %s, CI uses LLVM %s.\n' '$(TIDY)' "$$actual" '$(LLVM_VERSION)'; \
	   printf '*** clang-tidy gains and drops checks between releases, so an older\n'; \
	   printf '*** binary runs a WEAKER gate than CI while reporting clean, and can\n'; \
	   printf '*** also emit false positives CI never sees. This result is advisory.\n'; \
	   printf '***   make tidy TIDY=clang-tidy-%s   (if installed locally)\n' '$(LLVM_VERSION)'; \
	   printf '***   make verify-ci                 (CI versions, in a container)\n\n'; \
	 fi
	$(TIDY) -p $(BUILD_DIR) --warnings-as-errors='*' $(TIDY_SRC)

# --- cppcheck ----------------------------------------------------------------
# A target rather than a command spelled out in the workflow AND in verify-ci.
# Two copies of an analyser invocation is the same drift hazard sources.txt
# exists to prevent, and it had already started: the suppression list below was
# added in one place and would have been missing from the other.
#
# Two checks are suppressed FOR TESTS ONLY. Both fire on deliberate patterns,
# and cppcheck is right about the code in each case -- it is wrong about the
# intent. Neither is suppressed in src/, where both would be genuine bugs.
#
# knownConditionTrueFalse
#   Several tests pin wire-format constants by asserting their literal values --
#   `ERROR_NO_RESPONSE == 0x0120`. cppcheck correctly observes this is always
#   true. It is always true *because the constant is currently right*; the
#   assertion exists so that changing the constant fails the build.
#
# knownArgument
#   The exhaustive sweeps assert only on mismatch:
#
#       if (got != want) { INFO(...); REQUIRE(got == want); }
#
#   so the REQUIRE is reached only when it is going to fail, and its argument is
#   indeed always false there. That is the point of the shape: a million passing
#   CHECKs would make the suite slow and its assertion count meaningless, while
#   one REQUIRE on the first divergence -- with the offending input in the
#   message -- is what a failure actually needs. Rewriting it to satisfy the
#   check would mean asserting unconditionally, which is the thing being avoided.
#
# Vendored Catch2 is excluded by pointing cppcheck at src and tests only; it
# arrives through -isystem and this project is forbidden to edit it, so findings
# there could not be acted on.
CPPCHECK ?= cppcheck
CPPCHECK_FLAGS := --error-exitcode=1 --enable=warning,style,performance,portability \
                  --inline-suppr --std=c++11 --language=c++ \
                  --suppress=missingIncludeSystem \
                  --suppress=knownConditionTrueFalse:tests/* \
                  --suppress=knownArgument:tests/*

cppcheck:
	$(CPPCHECK) $(CPPCHECK_FLAGS) -I include src tests

format-check:
	$(FORMAT) --dry-run --Werror $(FORMAT_SRC)

format:
	$(FORMAT) -i $(FORMAT_SRC)

# --- CI parity ---------------------------------------------------------------
# The pre-push check: the gates whose LOCAL tool differs from CI's, run with
# CI's tools in CI's image.
#
# Every analysis tool a developer runs here is older than the one CI runs. On
# the WSL2 development host (Ubuntu 22.04) against the CI runner (Ubuntu 24.04):
#
#     clang-format   14  vs  20
#     clang-tidy     14  vs  20
#     cppcheck      2.7  vs  2.13
#
# That matters because a passing local run can mean "my tool does not know about
# this check" rather than "this code is fine" -- and in the other direction,
# formatting with clang-format 14 and having CI check with 20 produces a red
# build with no code change behind it. Both have already happened here.
#
# THE ANALYSERS COME FROM apt.llvm.org, NOT FROM UBUNTU. Ubuntu 24.04 packages
# clang 18 as its newest, and 20 finds things 18 does not -- clang-analyzer's
# unix.Stream checks caught a real fread-past-EOF loop in the platform tests
# that 18 waved through. The LLVM project's own apt repository is the only way
# to get 20 without maintaining a toolchain image, which this repository has
# decided not to do (see the note at the top of .github/workflows/cpp-ci.yml).
# Keep this recipe and that workflow on the SAME major version: the whole point
# of this target is that a green run here means a green run there.
#
# THE COMPILER IS PART OF THAT GAP, not just the analysers. The sanitizer tier
# runs here too, because the host's g++ 11 and CI's g++-14 do not emit the same
# warnings: g++-14 reports -Wmaybe-uninitialized against libstdc++'s <regex>
# internals under -O1 plus instrumentation, and g++ 11 does not. That turned the
# sanitizer job red on a branch whose local sanitizer run was green -- precisely
# the failure this target exists to prevent, missed because the target did not
# cover it.
#
# --cap-add=SYS_PTRACE is required for that tier: LeakSanitizer uses ptrace,
# containers block it by default, and LSan then dies with a fatal error that
# reads like a test failure. GitHub's runner is not a container and does not
# need it.
#
# Still not a substitute for the Valgrind or GCC 4.8.5 tiers. Run
# `make check-gcc48` separately -- it is the only thing that enforces C++11.
CI_BASE_IMAGE ?= docker.io/library/ubuntu:24.04

# The analyser major version, in ONE place. `.github/workflows/cpp-ci.yml` pins
# the same number as its workflow-level `env.LLVM_VERSION`; the two have to move
# together or this target stops predicting CI, which is its only job.
#
# That pairing is now ENFORCED rather than requested: `scripts/repo-hygiene.sh`
# reads both and fails when they disagree. It was a comment asking two files to
# be edited together, which is not a mechanism -- and the failure it invites is
# silent, because a local `verify-ci` on the wrong analyser still passes.
LLVM_VERSION ?= 22

verify-ci:
	@echo "Running the divergence-prone gates in $(CI_BASE_IMAGE) via $(CI_ENGINE)..."
	$(CI_ENGINE) run --rm --cap-add=SYS_PTRACE -v "$(CURDIR)":/cpp -w /cpp $(CI_BASE_IMAGE) bash -c '\
	  apt-get update -qq >/dev/null && \
	  apt-get install -y -qq --no-install-recommends \
	      g++-14 make cppcheck bear ca-certificates gnupg wget >/dev/null && \
	  wget -qO /tmp/llvm.key https://apt.llvm.org/llvm-snapshot.gpg.key && \
	  gpg --dearmor -o /usr/share/keyrings/llvm.gpg /tmp/llvm.key && \
	  echo "deb [signed-by=/usr/share/keyrings/llvm.gpg] http://apt.llvm.org/noble/ llvm-toolchain-noble-$(LLVM_VERSION) main" > /etc/apt/sources.list.d/llvm.list && \
	  apt-get update -qq >/dev/null && \
	  apt-get install -y -qq --no-install-recommends \
	      clang-format-$(LLVM_VERSION) clang-tidy-$(LLVM_VERSION) >/dev/null && \
	  echo "--- versions ---" && \
	  g++-14 --version | head -1 && \
	  clang-format-$(LLVM_VERSION) --version && \
	  clang-tidy-$(LLVM_VERSION) --version | grep -i version | head -1 && \
	  cppcheck --version && \
	  echo "--- format ---" && \
	  make format-check FORMAT=clang-format-$(LLVM_VERSION) && \
	  echo "--- build (CI compiler) ---" && \
	  make clean CXX=g++-14 && bear -- make -j$(CI_JOBS) all CXX=g++-14 && \
	  echo "--- clang-tidy ---" && \
	  make tidy TIDY=clang-tidy-$(LLVM_VERSION) CXX=g++-14 && \
	  echo "--- cppcheck ---" && \
	  make cppcheck && \
	  echo "--- sanitizers (CI compiler) ---" && \
	  make -j$(CI_JOBS) check SANITIZE=1 CXX=g++-14'
	@echo ""
	@echo "verify-ci OK. This leaves the build tree built with g++-14;"
	@echo "your next local 'make check' will rebuild against the host compiler."
