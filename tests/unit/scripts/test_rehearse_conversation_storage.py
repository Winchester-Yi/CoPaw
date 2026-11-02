# -*- coding: utf-8 -*-
"""Rehearsal evidence must reflect observed failures and actual processes."""

import importlib.util
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts/rehearse_conversation_storage.py"
)


def load_script():
    spec = importlib.util.spec_from_file_location(
        "conversation_rehearsal",
        SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nas_faults_use_fresh_generated_prefix_and_preserve_existing(tmp_path):
    module = load_script()
    sentinel = tmp_path / "production-session"
    sentinel.write_text("untouched")
    root = module.generated_root(tmp_path, str(uuid4()))
    root.mkdir()
    result = module.verify_nas(root)
    assert result["exclusive_publication"] == "passed"
    assert result["file_fsync_failure"] == "passed"
    assert result["directory_fsync_failure"] == "passed"
    assert result["truncated_body"] == "passed"
    assert result["missing_body"] == "passed"
    assert result["unreadable_body"] == "passed"
    assert sentinel.read_text() == "untouched"
    assert all(path.is_relative_to(root) for path in root.rglob("*"))


def test_generated_root_rejects_operator_paths(tmp_path):
    module = load_script()
    for invalid in ("../sessions", "/production", "arbitrary-prefix"):
        with pytest.raises(ValueError):
            module.generated_root(tmp_path, invalid)


def test_stage_preview_does_not_execute_and_apply_stops_at_failure(tmp_path):
    module = load_script()
    marker = tmp_path / "must-not-run"
    stages = [
        {"name": name, "argv": [sys.executable, "-c", "pass"]}
        for name in module.STAGES
    ]
    stages[1]["argv"] = [sys.executable, "-c", "raise SystemExit(7)"]
    stages[2]["argv"] = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
    ]
    manifest = tmp_path / "stages.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "service_unit_id": "schema-test-service",
                "scope": "isolated-copy",
                "stages": stages,
            },
        ),
    )
    preview = module.rehearse(manifest, tmp_path / "preview", apply=False)
    assert preview["status"] == "preview"
    assert not marker.exists()
    result = module.rehearse(manifest, tmp_path / "run", apply=True)
    assert result["status"] == "failed"
    assert [stage["exit_code"] for stage in result["stages"]] == [0, 7]
    assert result["stages"][1]["peak_rss_bytes"] > 0
    assert result["stages"][1]["seconds"] >= 0
    assert not marker.exists()
    assert json.loads((tmp_path / "run/report.json").read_text()) == result


def test_stages_reject_production_scope_or_missing_drain(tmp_path):
    module = load_script()
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"scope": "production", "stages": []}))
    with pytest.raises(ValueError):
        module.rehearse(path, tmp_path / "out", apply=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "service_unit_id": "schema-test-service",
                "scope": "isolated-copy",
                "stages": [],
            },
        ),
    )
    with pytest.raises(ValueError):
        module.rehearse(path, tmp_path / "out", apply=True)


def cluster_fixture():
    job_uid = str(uuid4())
    image = "registry/image@sha256:" + "a" * 64
    mount = {"name": "verification", "mountPath": "/mnt/verification"}
    container = {"name": "verifier", "image": image, "volumeMounts": [mount]}
    job = {
        "metadata": {"name": "job", "uid": job_uid},
        "spec": {
            "completionMode": "Indexed",
            "completions": 2,
            "template": {"spec": {"containers": [container]}},
        },
    }
    pods, reports = [], {}
    for index in range(2):
        name = f"pod-{index}"
        pods.append(
            {
                "metadata": {
                    "name": name,
                    "annotations": {
                        "batch.kubernetes.io/job-completion-index": str(index),
                    },
                    "ownerReferences": [
                        {
                            "kind": "Job",
                            "name": "job",
                            "uid": job_uid,
                            "controller": True,
                        },
                    ],
                },
                "spec": {
                    "nodeName": f"node-{index}",
                    "containers": [dict(container)],
                    "volumes": [
                        {
                            "name": "verification",
                            "persistentVolumeClaim": {
                                "claimName": "nas-verification",
                            },
                        },
                    ],
                },
                "status": {
                    "containerStatuses": [
                        {
                            "name": "verifier",
                            "imageID": image,
                            "state": {"terminated": {"exitCode": 0}},
                        },
                    ],
                },
            },
        )
        reports[name] = {
            "status": "passed",
            "scope": "indexed-nas",
            "job_uid": job_uid,
            "worker_index": index,
            "pod": name,
            "node": f"node-{index}",
            "operation_key": "b" * 64,
            "test_prefix": f"/mnt/verification/.swe-conversation-verification-{job_uid}",
            "mount_path": "/mnt/verification",
            "mountinfo": "42 31 0:52 / /mnt/verification rw - nfs nas:/test rw",
            "publication_outcome": (
                "published" if index == 0 else "exclusive-conflict"
            ),
            "cross_pod_body": "worker-0",
            "exit_code": 0,
            "nas_primitives": {
                key: "passed"
                for key in (
                    "file_and_parent_fsync",
                    "exclusive_publication",
                    "file_fsync_failure",
                    "directory_fsync_failure",
                    "truncated_body",
                    "missing_body",
                    "unreadable_body",
                )
            },
        }
    return job, pods, reports


def run_collector(module, monkeypatch, output, fixture):
    job, pods, reports = fixture

    def kubectl(namespace, *args):
        assert namespace == "isolated"
        if args[0] == "logs":
            return json.dumps(reports[args[1]])
        if args[1] == "job":
            return json.dumps(job)
        if args[1] == "pods":
            return json.dumps({"items": pods})
        if args[1] == "pvc":
            return json.dumps({"spec": {"volumeName": args[2]}})
        if args[1] == "pv":
            return json.dumps({"spec": {"storageClassName": "nas"}})
        return json.dumps({"metadata": {"name": "nas"}})

    monkeypatch.setattr(module, "_kubectl", kubectl)
    return module.collect("isolated", "job", output)


def test_cluster_collector_accepts_matching_indexed_job_evidence(
    tmp_path,
    monkeypatch,
):
    module = load_script()
    assert (
        run_collector(
            module,
            monkeypatch,
            tmp_path / "good",
            cluster_fixture(),
        )["status"]
        == "passed"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "status-only",
        "local-only",
        "different-prefix",
        "different-operation",
        "duplicate-index",
        "unmounted-pvc",
        "different-pvc",
        "wrong-owner",
        "wrong-job-uid",
        "wrong-pod",
        "wrong-node",
        "two-winners",
        "wrong-body",
        "different-body",
        "mutable-image",
        "different-imageid",
        "runtime-digest-mismatch",
        "failed-exit",
        "missing-primitive",
        "no-mountinfo",
    ],
)
def test_cluster_collector_rejects_incomplete_or_unrelated_evidence(
    tmp_path,
    monkeypatch,
    fault,
):
    module = load_script()
    fixture = cluster_fixture()
    job, pods, reports = fixture
    report = reports["pod-1"]
    if fault == "status-only":
        reports["pod-1"] = {"status": "passed"}
    elif fault == "local-only":
        report["scope"] = "local-only"
    elif fault == "different-prefix":
        report["test_prefix"] += "-unrelated"
    elif fault == "different-operation":
        report["operation_key"] = "c" * 64
    elif fault == "duplicate-index":
        pods[1]["metadata"]["annotations"][
            "batch.kubernetes.io/job-completion-index"
        ] = "0"
        report["worker_index"] = 0
    elif fault == "unmounted-pvc":
        pods[1]["spec"]["containers"] = [
            {
                "name": "verifier",
                "image": job["spec"]["template"]["spec"]["containers"][0][
                    "image"
                ],
            },
        ]
    elif fault == "different-pvc":
        pods[1]["spec"]["volumes"][0]["persistentVolumeClaim"][
            "claimName"
        ] = "other"
    elif fault == "wrong-owner":
        pods[1]["metadata"]["ownerReferences"][0]["uid"] = str(uuid4())
    elif fault == "wrong-job-uid":
        report["job_uid"] = str(uuid4())
    elif fault == "wrong-pod":
        report["pod"] = "other"
    elif fault == "wrong-node":
        report["node"] = "other"
    elif fault == "two-winners":
        report["publication_outcome"] = "published"
    elif fault == "wrong-body":
        for value in reports.values():
            value["cross_pod_body"] = "worker-1"
    elif fault == "different-body":
        report["cross_pod_body"] = "worker-1"
    elif fault == "mutable-image":
        pods[1]["spec"]["containers"][0]["image"] = "registry/image:latest"
    elif fault == "different-imageid":
        pods[1]["status"]["containerStatuses"][0]["imageID"] = (
            "registry/image@sha256:" + "d" * 64
        )
    elif fault == "runtime-digest-mismatch":
        for pod in pods:
            pod["status"]["containerStatuses"][0]["imageID"] = (
                "registry/image@sha256:" + "d" * 64
            )
    elif fault == "failed-exit":
        pods[1]["status"]["containerStatuses"][0]["state"]["terminated"][
            "exitCode"
        ] = 1
    elif fault == "missing-primitive":
        report["nas_primitives"].pop("file_and_parent_fsync")
    elif fault == "no-mountinfo":
        report["mountinfo"] = ""
    assert (
        run_collector(module, monkeypatch, tmp_path / fault, fixture)["status"]
        == "unverified"
    )


def test_indexed_runner_rejects_mutable_image_before_any_nas_write(
    tmp_path,
    monkeypatch,
):
    module = load_script()
    monkeypatch.setattr(
        sys,
        "argv",
        ["rehearse", "nas", "--mount", str(tmp_path), "--indexed"],
    )
    monkeypatch.setenv("VERIFICATION_IMAGE", "registry/image:latest")
    monkeypatch.setenv("JOB_UID", str(uuid4()))
    monkeypatch.setenv("JOB_COMPLETION_INDEX", "0")
    assert module.main() == 1
    assert list(tmp_path.iterdir()) == []
