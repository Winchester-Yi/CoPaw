#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Isolated conversation-storage NAS checks and measured outage rehearsals.

Never deploys Kubernetes resources. NAS writes use a generated UUID prefix.
Rehearsal commands are operator supplied argv arrays, previewed by default.
"""

import argparse
import errno
import json
import os
import re
from pathlib import Path
import stat
import subprocess
import sys
import time
from unittest.mock import patch
from uuid import UUID, uuid4

from swe.app.conversation_storage.bundle import (
    BundleBlock,
    BundleStore,
    new_operation_key,
)
from swe.app.conversation_storage.errors import IntegrityError

STAGES = (
    "drain",
    "stop_writers",
    "backup",
    "import",
    "verify",
    "install",
    "startup",
    "smoke",
    "restore_traffic",
)


def generated_root(mount, run_id):
    """Only generated Job UIDs/local UUIDs can select the writable prefix."""
    identifier = str(UUID(run_id))
    return Path(mount) / (".swe-conversation-verification-" + identifier)


def write_json(path, value):
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _expect_failure(operation, errors):
    try:
        operation()
    except errors:
        return "passed"
    raise RuntimeError("fault was not rejected")


def _publish(store, key, body=b"synthetic verification body"):
    return store.publish(key, [BundleBlock("body", "message", body)])


def _fsync_fault(store, directory):
    original = os.fsync
    key = new_operation_key()
    store.prepare_directory(key)
    directory_calls = 0

    def injected(descriptor):
        nonlocal directory_calls
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        directory_calls += int(is_directory)
        if (directory and is_directory and directory_calls == 3) or (
            not directory and not is_directory
        ):
            raise OSError(errno.EIO, "injected fsync failure")
        return original(descriptor)

    with patch("os.fsync", injected):
        result = _expect_failure(lambda: _publish(store, key), OSError)
    if directory:
        # The final link exists after a lost directory durability response;
        # same-operation reconciliation re-fsyncs it, never overwrites it.
        store.confirm_durable(store.path_for(key))
    return result


def verify_nas(root):
    """Exercise the actual bundle implementation; no SQL/model/tool calls."""
    store = BundleStore(root)
    key = new_operation_key()
    index = _publish(store, key)
    result = {
        "file_and_parent_fsync": "passed",
        "exclusive_publication": _expect_failure(
            lambda: _publish(store, key, b"replacement"),
            FileExistsError,
        ),
        "file_fsync_failure": _fsync_fault(store, False),
        "directory_fsync_failure": _fsync_fault(store, True),
    }
    extent = index.blocks["body"]
    assert store.read_extent(index.relative_path, extent.offset, extent.length)
    path = root / index.relative_path
    path.write_bytes(b"truncated")
    result["truncated_body"] = _expect_failure(
        lambda: store.inspect(index.relative_path),
        IntegrityError,
    )
    path.unlink()
    result["missing_body"] = _expect_failure(
        lambda: store.inspect(index.relative_path),
        FileNotFoundError,
    )
    # chmod is ineffective under root; inject the kernel's EACCES at open.
    original = os.open

    def denied(name, *args, **kwargs):
        if name == Path(index.relative_path).name:
            raise PermissionError(errno.EACCES, "injected NAS EACCES")
        return original(name, *args, **kwargs)

    with patch("os.open", denied):
        result["unreadable_body"] = _expect_failure(
            lambda: store.inspect(index.relative_path),
            PermissionError,
        )
    return result


def wait_for(path, timeout=120):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"peer did not publish {path.name}")
        time.sleep(0.05)


def indexed_nas(root, index):
    """Race two Pods on one operation, then read the winning complete body."""
    if index not in (0, 1):
        raise ValueError("indexed verification requires index 0 or 1")
    if index == 0:
        root.mkdir(mode=0o700)
        write_json(root / "operation.json", {"key": new_operation_key()})
    wait_for(root / "operation.json")
    key = json.loads((root / "operation.json").read_text())["key"]
    worker = root / f"worker-{index}"
    worker.mkdir(mode=0o700)
    primitives = verify_nas(worker)
    (root / f"ready-{index}").touch(exist_ok=False)
    wait_for(root / f"ready-{1-index}")
    store = BundleStore(root)
    try:
        _publish(store, key, f"worker-{index}".encode())
        outcome = "published"
    except FileExistsError:
        outcome = "exclusive-conflict"
    write_json(root / f"outcome-{index}.json", {"outcome": outcome})
    wait_for(root / f"outcome-{1-index}.json")
    peer = json.loads((root / f"outcome-{1-index}.json").read_text())
    if sorted([outcome, peer["outcome"]]) != [
        "exclusive-conflict",
        "published",
    ]:
        raise RuntimeError("exclusive publication did not produce one winner")
    bundle = store.confirm_durable(store.path_for(key))
    extent = bundle.blocks["body"]
    body = store.read_extent(
        bundle.relative_path,
        extent.offset,
        extent.length,
    )
    winner = index if outcome == "published" else 1 - index
    if body != f"worker-{winner}".encode():
        raise RuntimeError(
            "cross-Pod reader observed incomplete or replaced data",
        )
    return {
        "status": "passed",
        "scope": "indexed-nas",
        "job_uid": str(UUID(os.environ["JOB_UID"])),
        "operation_key": key,
        "mount_path": str(root.parent),
        "worker_index": index,
        "nas_primitives": primitives,
        "publication_outcome": outcome,
        "cross_pod_body": body.decode(),
        "test_prefix": str(root),
        "pod": os.environ.get("POD_NAME"),
        "node": os.environ.get("NODE_NAME"),
        "mountinfo": Path("/proc/self/mountinfo").read_text(),
        "exit_code": 0,
    }


def _stage(stage, output):
    started = time.monotonic()
    with (output / (stage["name"] + ".log")).open("xb") as log:
        with subprocess.Popen(
            stage["argv"],
            stdout=log,
            stderr=log,
        ) as process:
            _, status, usage = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
    return {
        "name": stage["name"],
        "argv": stage["argv"],
        "exit_code": process.returncode,
        "seconds": time.monotonic() - started,
        "peak_rss_bytes": int(
            usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
        ),
    }


def rehearse(manifest, output, *, apply=False):
    """Run a reviewed isolated-copy manifest; halt on first failed stage."""
    plan = json.loads(Path(manifest).read_text())
    if plan.get("scope") != "isolated-copy":
        raise ValueError(
            "only explicitly isolated-copy rehearsals are supported",
        )
    stages = plan.get("stages", [])
    names = tuple(stage["name"] for stage in stages)
    if names not in (STAGES, STAGES + ("rollback",)):
        raise ValueError(
            "all outage stages must occur in the documented order",
        )
    for stage in stages:
        argv = stage.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(arg, str) for arg in argv)
        ):
            raise ValueError(
                "each stage requires a nonempty argv string array",
            )
    report = {"scope": plan["scope"], "status": "preview", "stages": stages}
    if not apply:
        return report
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report.update(
        status="passed",
        stages=[],
        metrics={
            "db_lock_wait_seconds": None,
            "query_p95_seconds": None,
            "query_p99_seconds": None,
            "nas_bytes": None,
            "nas_files": None,
        },
    )
    started = time.monotonic()
    try:
        for stage in stages:
            measured = _stage(stage, output)
            report["stages"].append(measured)
            if measured["exit_code"] != 0:
                report["status"] = "failed"
                break
    except Exception as error:
        report.update(
            status="failed",
            error=f"{type(error).__name__}: {error}",
        )
    finally:
        report["total_seconds"] = time.monotonic() - started
        report["rss_semantics"] = (
            "wait4 process high-water RSS; not sum of concurrent descendants"
        )
        write_json(output / "report.json", report)
    return report


def _kubectl(namespace, *args):
    result = subprocess.run(
        ["kubectl", "-n", namespace, *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _named(items, name):
    matches = [item for item in items if item.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {name}")
    return matches[0]


def _job_identity(job):
    uid = str(UUID(job["metadata"]["uid"]))
    spec = job["spec"]
    if spec.get("completionMode") != "Indexed" or spec.get("completions") != 2:
        raise ValueError("expected two-completion Indexed Job")
    verifier = _named(spec["template"]["spec"]["containers"], "verifier")
    image = verifier["image"]
    if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("Job verifier image is not immutable")
    return uid, image


def _mounted_claim(pod, verifier, report):
    mount_path = report["mount_path"]
    if mount_path != "/mnt/verification":
        raise ValueError("unexpected verification mount path")
    matches = [
        mount
        for mount in verifier.get("volumeMounts", [])
        if mount.get("mountPath") == mount_path
    ]
    if len(matches) != 1:
        raise ValueError("verifier does not mount its reported NAS path")
    mount = matches[0]
    if (
        mount.get("readOnly")
        or mount.get("subPath")
        or mount.get("subPathExpr")
    ):
        raise ValueError(
            "verification requires one complete writable PVC mount",
        )
    volume = _named(pod["spec"]["volumes"], mount["name"])
    claim = volume["persistentVolumeClaim"]
    if claim.get("readOnly"):
        raise ValueError("verification PVC is read-only")
    lines = [line.split() for line in report["mountinfo"].splitlines()]
    if not any(
        len(line) > 6 and line[4] == mount_path and "rw" in line[5].split(",")
        for line in lines
    ):
        raise ValueError(
            "kernel mountinfo does not confirm the writable NAS mount",
        )
    return claim["claimName"]


def _pod_observation(pod, report, job):
    uid, image = _job_identity(job)
    if not isinstance(report, dict):
        raise ValueError("indexed report must be an object")
    if not pod["spec"].get("nodeName"):
        raise ValueError("Pod has no scheduled node")
    metadata = pod["metadata"]
    owner = [
        item
        for item in metadata.get("ownerReferences", [])
        if item.get("controller") is True
    ]
    if len(owner) != 1 or any(
        (
            owner[0].get("kind") != "Job",
            owner[0].get("name") != job["metadata"]["name"],
            owner[0].get("uid") != uid,
        ),
    ):
        raise ValueError("Pod controller does not match the actual Job UID")
    index = metadata["annotations"]["batch.kubernetes.io/job-completion-index"]
    if index not in ("0", "1"):
        raise ValueError("invalid Pod completion index")
    expected = {
        "status": "passed",
        "scope": "indexed-nas",
        "job_uid": uid,
        "pod": metadata["name"],
        "node": pod["spec"]["nodeName"],
        "worker_index": int(index),
        "exit_code": 0,
    }
    if any(report.get(key) != value for key, value in expected.items()):
        raise ValueError("indexed report identity does not match the Pod API")
    prefix = str(generated_root(Path(report["mount_path"]), uid))
    if report.get("test_prefix") != prefix:
        raise ValueError("report prefix is not generated from this Job UID")
    BundleStore.path_for(report["operation_key"])
    required = (
        "file_and_parent_fsync",
        "exclusive_publication",
        "file_fsync_failure",
        "directory_fsync_failure",
        "truncated_body",
        "missing_body",
        "unreadable_body",
    )
    if any(
        report.get("nas_primitives", {}).get(key) != "passed"
        for key in required
    ):
        raise ValueError("NAS primitive evidence is incomplete")
    verifier = _named(pod["spec"]["containers"], "verifier")
    status = _named(pod["status"]["containerStatuses"], "verifier")
    if (
        verifier.get("image") != image
        or status["state"]["terminated"]["exitCode"] != 0
    ):
        raise ValueError(
            "verifier image or actual termination is inconsistent",
        )
    image_id = status["imageID"]
    digest = re.search(r"sha256:[0-9a-f]{64}$", image_id)
    if digest is None or digest.group() != image.rsplit("@", 1)[1]:
        raise ValueError(
            "verifier runtime imageID does not match the fixed digest",
        )
    return {
        **expected,
        "image_id": image_id,
        "claim": _mounted_claim(pod, verifier, report),
        "prefix": prefix,
        "operation_key": report["operation_key"],
        "outcome": report["publication_outcome"],
        "body": report["cross_pod_body"],
    }


def _same_indexed_run(observations):
    if len(observations) != 2:
        return False
    same_fields = ("image_id", "claim", "prefix", "operation_key", "body")
    if any(
        len({row[key] for row in observations}) != 1 for key in same_fields
    ):
        return False
    if {row["worker_index"] for row in observations} != {0, 1}:
        return False
    if len({row["node"] for row in observations}) != 2:
        return False
    if sorted(row["outcome"] for row in observations) != [
        "exclusive-conflict",
        "published",
    ]:
        return False
    winner = next(row for row in observations if row["outcome"] == "published")
    return winner["body"] == f"worker-{winner['worker_index']}"


def collect(namespace, job, output):
    """Capture and cross-check actual Job, Pod, mount and indexed NAS evidence."""
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    job_api = json.loads(_kubectl(namespace, "get", "job", job, "-o", "json"))
    pods = json.loads(
        _kubectl(
            namespace,
            "get",
            "pods",
            "-l",
            f"job-name={job}",
            "-o",
            "json",
        ),
    )
    evidence = {
        "namespace": namespace,
        "job": job_api,
        "pods": pods,
        "storage": [],
        "rejections": [],
        "observations": [],
    }
    for pod in pods["items"]:
        name = pod["metadata"]["name"]
        log = _kubectl(namespace, "logs", name, "-c", "verifier")
        (output / (name + ".log")).write_text(log)
        try:
            report = json.loads(log.splitlines()[-1])
            observed = _pod_observation(pod, report, job_api)
            evidence["observations"].append(observed)
        except (KeyError, ValueError, TypeError, IndexError) as error:
            evidence["rejections"].append({"pod": name, "reason": str(error)})
            continue
        pvc = json.loads(
            _kubectl(namespace, "get", "pvc", observed["claim"], "-o", "json"),
        )
        pv = json.loads(
            _kubectl(
                namespace,
                "get",
                "pv",
                pvc["spec"]["volumeName"],
                "-o",
                "json",
            ),
        )
        storage_class = json.loads(
            _kubectl(
                namespace,
                "get",
                "storageclass",
                pv["spec"]["storageClassName"],
                "-o",
                "json",
            ),
        )
        evidence["storage"].append(
            {"pvc": pvc, "pv": pv, "storageClass": storage_class},
        )
    passed = not evidence["rejections"] and _same_indexed_run(
        evidence["observations"],
    )
    evidence["status"] = "passed" if passed else "unverified"
    write_json(output / "cluster-evidence.json", evidence)
    return {"status": evidence["status"], "output": str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    nas = commands.add_parser("nas")
    nas.add_argument("--mount", type=Path, required=True)
    nas.add_argument("--indexed", action="store_true")
    rehearsal = commands.add_parser("rehearse")
    rehearsal.add_argument("--manifest", type=Path, required=True)
    rehearsal.add_argument("--output", type=Path, required=True)
    rehearsal.add_argument("--apply", action="store_true")
    capture = commands.add_parser("collect")
    capture.add_argument("--namespace", required=True)
    capture.add_argument("--job", required=True)
    capture.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.mode == "nas":
            if args.indexed and not re.fullmatch(
                r"[^@\s]+@sha256:[0-9a-f]{64}",
                os.environ.get("VERIFICATION_IMAGE", ""),
            ):
                raise ValueError(
                    "VERIFICATION_IMAGE must be an immutable image digest",
                )
            root = generated_root(
                args.mount,
                os.environ["JOB_UID"] if args.indexed else str(uuid4()),
            )
            if args.indexed:
                report = indexed_nas(
                    root,
                    int(os.environ["JOB_COMPLETION_INDEX"]),
                )
            else:
                root.mkdir(mode=0o700)
                report = {
                    "status": "passed",
                    "scope": "local-only",
                    "test_prefix": str(root),
                    "nas_primitives": verify_nas(root),
                }
        elif args.mode == "rehearse":
            report = rehearse(args.manifest, args.output, apply=args.apply)
        else:
            report = collect(args.namespace, args.job, args.output)
    except Exception as error:
        report = {
            "status": "failed",
            "exit_code": 1,
            "error": f"{type(error).__name__}: {error}",
        }
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] in ("passed", "preview") else 1


if __name__ == "__main__":
    raise SystemExit(main())
