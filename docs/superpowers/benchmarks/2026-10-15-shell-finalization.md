# Shell finalization benchmark — 2026-10-15

Delivered: stage A only. Finalization computes `(start, end)` once per stream and passes those local immutable tuples through nonempty checks and decoding. Strict UTF-8 prevalidation and whole-stream locale fallback remain unchanged.

Stage B was implemented and passed its correctness tests, then rejected by the approved performance gate. The optimistic strict UTF-8 writer rewound/truncated the current stream segment on decoding failure and rewrote that entire segment using the existing preferred locale encoding with replacement. It reduced valid UTF-8 content reads from 2N to N, but invalid-tail finalization consistently exceeded the permitted 20% regression. No optimistic retry code remains in production.

## Method

Baseline is `d2963d586` (full commit and source hashes in the JSON). `git show` loads an isolated baseline source copy under `/private/tmp`; the checkout is never switched. Measurements use the project venv and local temporary filesystem. Each 8/64 MiB case has two excluded warmups and 12 measured repetitions per variant, alternating execution order. Both stdout and stderr are tested for ASCII, a final invalid UTF-8 byte, and all-newline input. The rejected candidate was measured in two separate rounds. Every iteration verifies exact artifact bytes against the existing decoder/formatter, with fsync and rename enabled.

The probes distinguish collection, boundary location, content-read passes/bytes, transformation (input read + decode/encode + marker scanning, excluding timed target writes), artifact writes, fsync, rename, display, close, and total wall/CPU time. They are synchronous finalization probes: output-worker queueing, concurrent loop p95/p99, peak RSS, container memory, real subprocess collection, and deployment storage are not measured. Small differences around 1% are treated as noise. These are local relative comparisons, not storage latency promises.

Reproduce the accepted implementation:

```bash
venv/bin/python scripts/benchmark_shell_finalization.py \
  --baseline d2963d586 --sizes-mib 8 64 --repeats 12 \
  --output /private/tmp/shell-finalization.json
```

The JSON contains per-stage medians, individual finalization samples, and the rejected candidate source. Extract that source to a temporary file and pass `--candidate-source` to reproduce the rejected variant without editing the checkout.

## Accepted bounds-only result

Median total finalization times; each row is 12 samples per variant. Ratios below 1 mean faster.

| Input | Baseline | Bounds-only | Ratio |
|---|---:|---:|---:|
| 8 MiB ASCII stdout | 5.26 ms | 5.22 ms | 0.991 |
| 8 MiB ASCII stderr | 16.06 ms | 16.05 ms | 0.999 |
| 8 MiB invalid tail stdout | 5.32 ms | 5.38 ms | 1.011 |
| 8 MiB invalid tail stderr | 16.22 ms | 16.11 ms | 0.993 |
| 8 MiB newlines stdout | 28.46 ms | 14.38 ms | 0.505 |
| 8 MiB newlines stderr | 28.37 ms | 14.29 ms | 0.504 |
| 64 MiB ASCII stdout | 30.31 ms | 30.31 ms | 1.000 |
| 64 MiB ASCII stderr | 117.63 ms | 117.49 ms | 0.999 |
| 64 MiB invalid tail stdout | 30.99 ms | 30.27 ms | 0.977 |
| 64 MiB invalid tail stderr | 118.05 ms | 119.39 ms | 1.011 |
| 64 MiB newlines stdout | 226.21 ms | 113.67 ms | 0.503 |
| 64 MiB newlines stderr | 226.31 ms | 113.68 ms | 0.502 |

All cases use exactly two boundary calls in the accepted implementation. Nonempty cases previously used five; all-newline cases used four. All-newline boundary reads fall from 2N to N. Valid/invalid-tail content reads remain 2N, preserving the prevalidation strategy. The improvement is concentrated in newline-heavy boundary scans; ordinary ASCII and invalid-tail timings are effectively unchanged.

## Rejected optimistic result

| Invalid-tail case | Round 1 candidate/baseline | Round 2 candidate/baseline |
|---|---:|---:|
| 8 MiB stdout | 1.247 | 1.283 |
| 8 MiB stderr | 1.769 | 1.745 |
| 64 MiB stdout | 1.966 | 1.972 |
| 64 MiB stderr | 2.005 | 2.009 |

Valid ASCII stdout improved by 17–28%, and stderr by 5–6%, but invalid-tail artifact writes approached 2N because the already-written prefix was discarded and rewritten. Stderr repeated its marker scans as well. Both rounds exceed the 20% gate on every invalid-tail case, so normal UTF-8 gains do not justify shipping stage B.

## Verification

TDD observed the bounds-once test fail against the original implementation (four/five calls instead of two), then pass after stage A. The candidate's single-read test failed at 411,070 vs 205,535 bytes before stage B; its truncate-failure test also failed before the retry path existed. Stage B passed 38 spool tests before rejection; its single-read/truncate-specific tests were then removed or adjusted to the retained prevalidation behavior.

Final regression coverage includes Unicode split across 64 KiB chunks, long/all-newline stripping, invalid UTF-8 at the start/middle/end and incomplete tail, mocked cp1252 whole-stream fallback, dual streams, timeout suffix and diagnostic preservation, UTF-16 fallback marker classification, exact case-sensitive/cross-chunk stderr markers, durability-failure full-response fallback, and existing cancellation/file-ownership tests. No append validation state, process pool, persistence weakening, or changed full-string failure semantics was introduced.
