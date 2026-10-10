// SPDX-License-Identifier: Apache-2.0
//
// The executable entry point, and nothing else.
//
// Everything the CLI does lives in `mie/cli.hpp`, which takes a vector of
// arguments rather than `(argc, argv)`. That is what makes the exit-code
// contract testable in-process: the Catch2 suite links the library, so a test
// that had to spawn this binary to check an exit code would be a subprocess per
// case and could not run at all under the sanitizer or GCC 4.8.5 tiers.

#include <string>
#include <vector>

#include "mie/cli.hpp"
#include "mie/platform.hpp"

int main(int argc, char** argv) {
    // First, before anything writes a byte. On Windows the CRT otherwise
    // rewrites every newline into CRLF on the way out, which breaks byte-exact
    // output on that platform alone (L2-WRT-012). On POSIX a closed downstream
    // pipe would otherwise kill the process with SIGPIPE instead of exiting 0
    // (L2-WRT-018).
    mie::platform::prepare_output_streams();

    // Through the platform layer, not straight off argv. On Windows the CRT
    // built argv in the ANSI codepage, so a path containing any character that
    // codepage cannot represent arrived as `?` before this line ran -- and no
    // care further down could recover it.
    return mie::cli::run(mie::platform::command_line_arguments(argc, argv));
}
