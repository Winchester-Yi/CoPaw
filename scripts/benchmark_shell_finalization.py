#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare Shell finalization with an isolated source copy from a git ref.

Run with the project venv. This is a synchronous finalization microbenchmark;
it does not measure concurrent event-loop latency or output-worker queueing.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib.util
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import tempfile
import time
from unittest.mock import patch

from swe.agents.tools import shell_output_spool as current
from swe.agents.tools.shell import smart_decode, _format_shell_response


@contextmanager
def measured(module, metrics):
    """Keep probes outside production and distinguish input reads from writes."""
    original_bounds = module._stream_bounds
    original_chunks = module._read_chunks
    original_fsync = module.os.fsync
    original_open = Path.open
    original_replace = Path.replace
    original_display = module._historical_artifact_display
    original_stream = module.ShellOutputSpool._write_stream

    class Reader:
        def __init__(self, file):
            self.file = file

        def __getattr__(self, name):
            return getattr(self.file, name)

        def read(self, size=-1):
            data = self.file.read(size)
            metrics["bounds_read_bytes"] += len(data)
            return data

    class Target:
        def __init__(self, file):
            self.file = file

        def __getattr__(self, name):
            return getattr(self.file, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

        def write(self, data):
            start = time.perf_counter()
            result = self.file.write(data)
            metrics["artifact_write_s"] += time.perf_counter() - start
            metrics["artifact_write_bytes"] += len(data)
            return result

        def truncate(self, *args):
            metrics["segment_rewrites"] += 1
            return self.file.truncate(*args)

    def write_stream(spool, *args):
        start = time.perf_counter()
        before = metrics["artifact_write_s"]
        try:
            return original_stream(spool, *args)
        finally:
            metrics["stream_transform_s"] += (
                time.perf_counter()
                - start
                - (metrics["artifact_write_s"] - before)
            )

    def bounds(reader):
        metrics["bounds_calls"] += 1
        start = time.perf_counter()
        result = original_bounds(Reader(reader))
        metrics["bounds_s"] += time.perf_counter() - start
        return result

    def chunks(reader, limits):
        metrics["content_read_passes"] += 1
        for chunk in original_chunks(reader, limits):
            metrics["content_read_bytes"] += len(chunk)
            yield chunk

    def fsync(fd):
        start = time.perf_counter()
        result = original_fsync(fd)
        metrics["fsync_s"] += time.perf_counter() - start
        return result

    def open_file(path, mode="r", *args, **kwargs):
        # pylint: disable=consider-using-with

        # The returned wrapper transfers ownership to the caller context.
        file = original_open(
            path,
            mode,
            *args,
            **kwargs,
        )
        return Target(file) if mode == "xb" else file

    def replace(path, target):
        start = time.perf_counter()
        result = original_replace(path, target)
        metrics["rename_s"] += time.perf_counter() - start
        return result

    def display(*args):
        start = time.perf_counter()
        result = original_display(*args)
        metrics["display_s"] += time.perf_counter() - start
        return result

    with (
        patch.object(module.ShellOutputSpool, "_write_stream", write_stream),
        patch.object(module, "_stream_bounds", bounds),
        patch.object(
            module,
            "_read_chunks",
            chunks,
        ),
        patch.object(module.os, "fsync", fsync),
        patch.object(
            Path,
            "open",
            open_file,
        ),
        patch.object(Path, "replace", replace),
        patch.object(
            module,
            "_historical_artifact_display",
            display,
        ),
    ):
        yield


def load_copy(source, path, name):
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location(
        f"swe.agents.tools.{name}",
        path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_one(module, directory, raw, source):
    metrics = dict.fromkeys(
        (
            "stream_transform_s",
            "bounds_calls",
            "bounds_read_bytes",
            "bounds_s",
            "content_read_passes",
            "content_read_bytes",
            "artifact_write_s",
            "artifact_write_bytes",
            "segment_rewrites",
            "fsync_s",
            "rename_s",
            "display_s",
        ),
        0,
    )
    spool = module.ShellOutputSpool(
        directory,
        4096,
        smart_decode,
        _format_shell_response,
    )
    start = time.perf_counter()
    spool.streams[source].append(raw)
    metrics["collect_s"] = time.perf_counter() - start
    try:
        stdout, stderr = spool.decode_streams()
        with measured(module, metrics):
            start = time.perf_counter()
            cpu_start = time.process_time()
            text, _ = spool.prepare_response(0, stdout, stderr, "")
            metrics["finalize_s"] = time.perf_counter() - start
            metrics["finalize_cpu_s"] = time.process_time() - cpu_start
        artifact = list(spool.directory.glob("*.txt"))
        expected = _format_shell_response(
            0,
            smart_decode(raw) if source == "stdout" else "",
            smart_decode(raw) if source == "stderr" else "",
        ).encode()
        actual = artifact[0].read_bytes() if artifact else text.encode()
        assert actual == expected
        metrics["final_bytes"] = len(actual)
        for path in artifact:
            path.unlink()
    finally:
        start = time.perf_counter()
        for stream in spool.streams.values():
            stream.close()
        metrics["close_s"] = time.perf_counter() - start
    return metrics


def compare_case(modules, root, raw, source, repeats):
    samples = {key: [] for key in modules}
    for repetition in range(repeats + 2):
        order = list(modules) if repetition % 2 else list(reversed(modules))
        for key in order:
            metrics = run_one(
                modules[key],
                root / key,
                raw,
                source,
            )
            if repetition >= 2:
                samples[key].append(metrics)
    summaries = {
        key: {
            field: statistics.median(row[field] for row in values)
            for field in values[0]
        }
        for key, values in samples.items()
    }
    ratio = (
        summaries["candidate"]["finalize_s"]
        / summaries["baseline"]["finalize_s"]
    )
    return samples, summaries, ratio


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="HEAD")
    parser.add_argument("--candidate-source", type=Path)
    parser.add_argument("--sizes-mib", type=int, nargs="+", default=[8, 64])
    parser.add_argument("--repeats", type=int, default=16)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline_source = subprocess.check_output(
        [
            "git",
            "show",
            f"{args.baseline}:src/swe/agents/tools/shell_output_spool.py",
        ],
    )
    rows = []
    with tempfile.TemporaryDirectory(
        prefix="shell-finalization-",
        dir="/private/tmp",
    ) as temp:
        root = Path(temp)
        baseline = load_copy(
            baseline_source,
            root / "baseline.py",
            "_benchmark_baseline",
        )
        candidate = (
            current
            if args.candidate_source is None
            else load_copy(
                args.candidate_source.read_bytes(),
                root / "candidate.py",
                "_benchmark_candidate",
            )
        )
        modules = {"baseline": baseline, "candidate": candidate}
        for size in args.sizes_mib:
            count = size * 1024 * 1024
            for scenario in ("ascii", "invalid_tail", "newlines"):
                raw = b"\n" * count if scenario == "newlines" else b"a" * count
                if scenario == "invalid_tail":
                    raw = raw[:-1] + b"\xff"
                for source in ("stdout", "stderr"):
                    samples, summaries, ratio = compare_case(
                        modules,
                        root,
                        raw,
                        source,
                        args.repeats,
                    )
                    rows.append(
                        {
                            "size_mib": size,
                            "scenario": scenario,
                            "source": source,
                            "ratio": ratio,
                            "medians": summaries,
                            "samples": samples,
                        },
                    )
                    print(
                        f"{size} MiB {scenario} {source}: candidate/baseline={ratio:.3f}",
                        flush=True,
                    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "baseline": args.baseline,
                "baseline_commit": subprocess.check_output(
                    ["git", "rev-parse", args.baseline],
                    text=True,
                ).strip(),
                "baseline_sha256": hashlib.sha256(baseline_source).hexdigest(),
                "candidate_sha256": hashlib.sha256(
                    (
                        args.candidate_source or Path(current.__file__)
                    ).read_bytes(),
                ).hexdigest(),
                "candidate_source": str(
                    args.candidate_source or current.__file__,
                ),
                "repeats": args.repeats,
                "warmups": 2,
                "alternating_order": True,
                "note": "Local filesystem, synchronous finalization only; no loop p99, RSS, or queue claims.",
                "results": rows,
            },
            indent=2,
        )
        + "\n",
    )


if __name__ == "__main__":
    main()
