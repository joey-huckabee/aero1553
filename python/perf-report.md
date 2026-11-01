## Performance report

aero1553 5.8.0 | Python 3.14.7 | Linux aarch64 | Arm implementer 0x41 part 0xd4f | 4 CPUs | 460,575 records (20.80 MB golden recording) | 3 runs per case

Times are seconds. **Best** is the fastest run; **Total** adds the median import time of every library the case uses (each measured in a fresh interpreter) to that best run. Process cases include interpreter start-up and imports already.

| Case | Phases (best run) | Runs | Best | Imports | Total | Records/s | Peak MB |
|---|---|---|---:|---|---:|---:|---:|
| `python.startup` |  | 0.013 0.013 0.012 | 0.012 |  | 0.012 |  |  |
| `import.aero1553` |  | 0.039 0.037 0.037 | 0.037 |  | 0.037 |  |  |
| `import.numpy` |  | 0.053 0.048 0.052 | 0.048 |  | 0.048 |  |  |
| `import.pandas` |  | 0.205 0.202 0.199 | 0.199 |  | 0.199 |  |  |
| `rust.iterate` |  | 0.051 0.051 0.051 | 0.051 |  | 0.051 | 9,090,416 | 2 |
| `rust.filter_order` |  | 0.077 0.077 0.077 | 0.077 |  | 0.077 | 5,791,812 | 2 |
| `rust.decode_csv` |  | 0.083 0.086 0.086 | 0.083 |  | 0.083 | 5,578,332 | 5 |
| `rust.merge_csv` |  | 0.187 0.198 0.196 | 0.187 |  | 0.187 | 3,824,397 | 6 |
| `rust.cli_process` |  | 0.084 0.090 0.090 | 0.084 |  | 0.084 | 5,457,826 | 15 |
| `rust.cli_no_mux` |  | 0.079 0.085 0.085 | 0.079 |  | 0.079 | 5,810,840 | 15 |
| `rust.cli_stdin` |  | 0.080 0.086 0.087 | 0.080 |  | 0.080 | 5,759,553 | 15 |
| `py.cli_process` |  | 0.144 0.148 0.147 | 0.144 |  | 0.144 | 3,201,845 | 24 |
| `py.cli_stdin` |  | 0.140 0.161 0.141 | 0.140 |  | 0.140 | 3,285,414 | 25 |
| `py.iterate` |  | 0.107 0.107 0.107 | 0.107 | aero1553 0.037 | 0.143 | 4,324,343 | 184 |
| `py.filter_order` |  | 0.131 0.131 0.131 | 0.131 | aero1553 0.037 | 0.168 | 3,399,032 | 184 |
| `py.decode_csv` |  | 0.085 0.092 0.091 | 0.085 | aero1553 0.037 | 0.122 | 5,404,956 | 184 |
| `py.cli_decode` |  | 0.084 0.087 0.087 | 0.084 | aero1553 0.037 | 0.121 | 5,493,282 | 184 |
| `py.merge_csv` |  | 0.196 0.213 0.209 | 0.196 | aero1553 0.037 | 0.233 | 3,661,574 | 184 |
| `py.columns_numpy` | columns 0.301, numpy 0.000 | 0.333 0.318 0.301 | 0.301 | aero1553 0.037, numpy 0.052 | 0.390 | 1,532,280 | 307 |
| `py.columns_pandas` | columns 0.293, dataframe 0.035 | 0.377 0.349 0.328 | 0.328 | aero1553 0.037, numpy 0.052, pandas 0.202 | 0.619 | 1,404,800 | 351 |
| `py.columns_pandas_numeric` | columns 0.079, dataframe 0.007 | 0.098 0.087 0.087 | 0.087 | aero1553 0.037, numpy 0.052, pandas 0.202 | 0.378 | 5,323,503 | 185 |
| `py.to_dict_dataclass` |  | 0.799 0.776 0.803 | 0.776 | aero1553 0.037 | 0.813 | 593,539 | 287 |
| `py.attrs_dataclass` |  | 0.779 0.755 0.786 | 0.755 | aero1553 0.037 | 0.792 | 610,281 | 287 |
| `py.to_dict_pandas` | to_dict 1.010, dataframe 0.938 | 2.014 1.997 1.948 | 1.948 | aero1553 0.037, pandas 0.202 | 2.187 | 236,448 | 1,085 |
| `py.attrs_pandas` | attributes 0.835, dataframe 0.311 | 1.153 1.177 1.146 | 1.146 | aero1553 0.037, pandas 0.202 | 1.385 | 401,933 | 563 |
| `py.csv_pandas` | decode+write 0.091, read_csv 1.156 | 1.261 1.246 1.247 | 1.246 | aero1553 0.037, pandas 0.202 | 1.485 | 369,540 | 395 |

### Gates (median of 7 interleaved paired rounds)

| Gate | Ratio | Rounds | Best / best | Limit | Result |
|---|---:|---:|---:|---|---|
| Python library decode keeps pace with the Rust library (`py.decode_csv` / `rust.decode_csv`) | 1.031 | 0.990-1.053 | 1.032 | max 1.15 | pass |
| Python CLI keeps pace with the Rust library (`py.cli_decode` / `rust.decode_csv`) | 1.013 | 0.982-1.041 | 1.015 | max 1.15 | pass |
| Python merge keeps pace with the Rust merge (`py.merge_csv` / `rust.merge_csv`) | 1.039 | 1.038-1.054 | 1.044 | max 1.15 | pass |
| Reading standard input costs no more than reading the file (`rust.cli_stdin` / `rust.cli_no_mux`) | 1.000 | 0.966-1.018 | 1.009 | max 1.25 | pass |
| Python record iteration stays cheap (`py.iterate` / `rust.iterate`) | 2.105 | 2.064-2.133 | 2.102 | max 3.0 | pass |
| columns() stays far ahead of per-record dicts (`py.to_dict_pandas` / `py.columns_pandas`) | 5.351 | 5.171-5.394 | 5.941 | min 4.0 | pass |
| numeric columns cost a small multiple of a bare Rust read (`py.columns_pandas_numeric` / `rust.iterate`) | 1.956 | 1.873-2.028 | 1.708 | max 3.5 | pass |

Every case's output matched the golden pins.
