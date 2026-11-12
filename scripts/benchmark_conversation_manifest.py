# -*- coding: utf-8 -*-
"""Measure streaming manifest progress mirrors; excludes migration/MySQL IO.

Run each entry count in a fresh process, for example --entries 10000, 20000,
and 40000. The fixture and output are streamed and compared entry by entry.
"""

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import tracemalloc

import psutil

from swe.app.conversation_storage.migration.disk_manifest import (
    disk_index_policy,
    load_disk_manifest,
    write_json,
)


def _fixture(path, entries):
    with path.open("w", encoding="utf-8") as stream:
        stream.write('{"version":3,"assignments":{"session":{')
        for index in range(entries):
            if index:
                stream.write(",")
            stream.write(json.dumps(str(index)))
            stream.write(":")
            stream.write(
                json.dumps({"target_id": index}, separators=(",", ":")),
            )
        stream.write('}},"batches":{')
        for index in range(entries):
            if index:
                stream.write(",")
            stream.write(json.dumps(str(index)))
            stream.write(':"committed"')
        stream.write("}}")


def _digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            result.update(chunk)
    return result.hexdigest()


def measure(entries):
    with tempfile.TemporaryDirectory(prefix="manifest-scale-") as directory:
        root = Path(directory).resolve()
        source, output = root / "source.json", root / "output.json"
        _fixture(source, entries)
        process = psutil.Process()
        baseline = process.memory_info().rss
        peak = [baseline]
        stop = threading.Event()

        def sample():
            while not stop.wait(0.005):
                peak[0] = max(peak[0], process.memory_info().rss)

        sampler = threading.Thread(target=sample, daemon=True)
        tracemalloc.start()
        sampler.start()
        started = time.perf_counter()
        try:
            with disk_index_policy(root, 256 * 1024 * 1024) as policy:
                value = load_disk_manifest(source)
                assert len(value["assignments"]["session"]) == entries
                assert len(value["batches"]) == entries
                with output.open("w", encoding="utf-8") as stream:
                    write_json(value, stream)
                actual = load_disk_manifest(output)
                assert value == actual
                index_peak = policy.peak_bytes
            peak[0] = max(peak[0], process.memory_info().rss)
            python_peak = tracemalloc.get_traced_memory()[1]
        finally:
            stop.set()
            sampler.join()
            tracemalloc.stop()
        return {
            "scope": "manifest parse/write only; excludes MySQL and NAS",
            "entries_per_mapping": entries,
            "total_progress_entries": entries * 2,
            "source_bytes": source.stat().st_size,
            "output_sha256": _digest(output),
            "semantic_roundtrip_equal": True,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "rss_baseline_bytes": baseline,
            "sampled_peak_rss_bytes": peak[0],
            "python_peak_bytes": python_peak,
            "index_peak_bytes": index_peak,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=int, default=10000)
    args = parser.parse_args()
    if args.entries < 1:
        parser.error("entries must be positive")
    print(json.dumps(measure(args.entries), sort_keys=True))


if __name__ == "__main__":
    main()
