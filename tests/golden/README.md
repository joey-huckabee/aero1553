# Golden recordings

Generated MIE recordings, pinned by hash, shared by the Python end-to-end
test (`python/tests/test_e2e_golden.py`) and the performance suite
(`perf/`).

MIE binaries are not committed (repository hygiene), and the performance
suite needs a recording far larger than any fixture, so the recordings are
*generated* by `golden.py`, deterministically from a fixed seed. Every record
is a real encoding: the templates are the records of the conformance fixtures
in `../conformance/inputs`, re-timed onto one clock. `golden.json` pins the
SHA-256 of every recording and of the CSV the CLI decodes it to (plus its row
count and RT checksum), so a change to the generator, a template fixture or
the decoder's output is a hash mismatch, not a quietly different benchmark.

| Recording | Contents |
|---|---|
| `a-small` | IRIG, every record kind, equal-timestamp runs, one corrupt region, day 365 into day 366 |
| `b-small` | a second recorder; one transaction in five duplicates one of `a-small`'s |
| `standard-small` | Standard (free-running counter) timestamps |
| `freerun-small` | IRIG with the clock not locked to a time source |
| `a-large` / `b-large` | `a-small` / `b-small` at scale (about 460,000 / 320,000 records) |

```bash
python tests/golden/golden.py --out DIR              # write every recording, checked against the pins
python tests/golden/golden.py --out DIR a-small      # just one
python tests/golden/golden.py --update --cli rust/target/release/aero1553   # re-pin (after an intended change)
```

Re-pinning is a deliberate act, like updating a conformance oracle: do it
only after the change that moved the hashes has been reviewed, and confirm
the other implementations agree (`aero1553 decode` from the Rust, Python and
C++ builds should produce the pinned CSV).
