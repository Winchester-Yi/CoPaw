# -*- coding: utf-8 -*-
import io
import os

import pytest

from swe.app.conversation_storage.bundle import (
    BundleBlock,
    BundleStore,
    new_operation_key,
)
from swe.app.conversation_storage.errors import IntegrityError


def test_bundle_roundtrip_has_byte_extents_and_no_body_in_index(tmp_path):
    store = BundleStore(tmp_path)
    operation = new_operation_key()
    body = "用户正文".encode("utf-8")
    result = store.publish(
        operation,
        [BundleBlock("message:0", "message", body)],
        metadata={"revision": 1},
    )
    assert result.relative_path == store.path_for(operation)
    assert result.metadata == {"revision": 1}
    extent = result.blocks["message:0"]
    assert extent.length == len(body)
    assert (
        store.read_extent(
            result.relative_path,
            extent.offset,
            extent.length,
            expected_length=result.byte_length,
        )
        == body
    )
    assert (
        body
        not in (tmp_path / result.relative_path).read_bytes()[: extent.offset]
    )
    assert store.inspect(result.relative_path) == result


def test_bundle_header_rejects_body_fields(tmp_path):
    with pytest.raises(ValueError, match="non-structural"):
        BundleStore(tmp_path).publish(
            new_operation_key(),
            [BundleBlock("root", "recovery", b"{}")],
            metadata={"message": "payload"},
        )


def test_publication_is_exclusive_and_preserves_original(tmp_path):
    store = BundleStore(tmp_path)
    operation = new_operation_key()
    original = store.publish(
        operation,
        [BundleBlock("root", "recovery", b"first")],
    )
    with pytest.raises(FileExistsError):
        store.publish(operation, [BundleBlock("root", "recovery", b"second")])
    extent = original.blocks["root"]
    assert (
        store.read_extent(original.relative_path, extent.offset, extent.length)
        == b"first"
    )


def test_bundle_streams_large_block_with_bounded_reads(tmp_path):
    class Source(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 65536
            return super().read(size)

    payload = b"x" * 200000
    store = BundleStore(tmp_path)
    result = store.publish(
        new_operation_key(),
        [BundleBlock("root", "recovery", Source(payload), len(payload))],
    )
    assert result.blocks["root"].length == len(payload)


@pytest.mark.parametrize(
    "offset,length",
    [(-1, 1), (0, -1), (0, 1000000), (True, 1)],
)
def test_extent_rejects_invalid_bounds(tmp_path, offset, length):
    store = BundleStore(tmp_path)
    result = store.publish(
        new_operation_key(),
        [BundleBlock("root", "recovery", b"{}")],
    )
    with pytest.raises(IntegrityError):
        store.read_extent(result.relative_path, offset, length)


@pytest.mark.parametrize(
    "path",
    [
        "../escape.bundle",
        "/tmp/escape.bundle",
        "bundles/aa/../../escape.bundle",
    ],
)
def test_reader_rejects_non_generated_paths(tmp_path, path):
    with pytest.raises(IntegrityError):
        BundleStore(tmp_path).inspect(path)


def test_reader_and_writer_reject_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "bundles").symlink_to(outside, target_is_directory=True)
    store = BundleStore(root)
    with pytest.raises(IntegrityError):
        store.publish("a" * 64, [BundleBlock("root", "recovery", b"{}")])
    with pytest.raises(IntegrityError):
        store.inspect(store.path_for("a" * 64))
    assert list(outside.iterdir()) == []


def test_truncated_bundle_and_unknown_version_fail_closed(tmp_path):
    store = BundleStore(tmp_path)
    result = store.publish(
        new_operation_key(),
        [BundleBlock("root", "recovery", b"payload")],
    )
    path = tmp_path / result.relative_path
    content = path.read_bytes()
    path.write_bytes(content[:-1])
    with pytest.raises(IntegrityError):
        store.inspect(result.relative_path)
    path.write_bytes(content[:8] + (99).to_bytes(4, "big") + content[12:])
    with pytest.raises(IntegrityError, match="version"):
        store.inspect(result.relative_path)


def test_failed_directory_fsync_keeps_exclusive_file_for_reconciliation(
    tmp_path,
    monkeypatch,
):
    store = BundleStore(tmp_path)
    operation = new_operation_key()
    store.prepare_directory(operation)
    original = os.fsync

    def fail_directory(fd):
        import stat

        if (
            stat.S_ISDIR(os.fstat(fd).st_mode)
            and (tmp_path / store.path_for(operation)).exists()
        ):
            raise OSError("directory fsync failed")
        original(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(OSError, match="directory fsync"):
        store.publish(operation, [BundleBlock("root", "recovery", b"{}")])
    assert (tmp_path / store.path_for(operation)).is_file()


def test_bundle_requires_a_provisioned_root(tmp_path):
    root = tmp_path / "unprovisioned"
    with pytest.raises(FileNotFoundError):
        BundleStore(root).publish(
            new_operation_key(),
            [BundleBlock("root", "recovery", b"{}")],
        )
    assert not root.exists()


def test_final_file_symlink_is_never_followed(tmp_path):
    store = BundleStore(tmp_path)
    operation = new_operation_key()
    store.prepare_directory(operation)
    target = tmp_path / "outside"
    target.write_bytes(b"secret")
    (tmp_path / store.path_for(operation)).symlink_to(target)
    with pytest.raises(IntegrityError):
        store.inspect(store.path_for(operation))
    with pytest.raises(FileExistsError):
        store.publish(operation, [BundleBlock("root", "recovery", b"{}")])
    assert target.read_bytes() == b"secret"


def test_file_fsync_failure_does_not_publish(tmp_path, monkeypatch):
    store = BundleStore(tmp_path)
    operation = new_operation_key()
    store.prepare_directory(operation)
    monkeypatch.setattr(
        os,
        "fsync",
        lambda _: (_ for _ in ()).throw(OSError("file fsync failed")),
    )
    with pytest.raises(OSError, match="file fsync"):
        store.publish(operation, [BundleBlock("root", "recovery", b"{}")])
    assert not (tmp_path / store.path_for(operation)).exists()


def test_large_extent_requires_streaming(tmp_path):
    from swe.app.conversation_storage.bundle import MAX_READ_BYTES

    store = BundleStore(tmp_path)
    payload = b"x" * (MAX_READ_BYTES + 1)
    result = store.publish(
        new_operation_key(),
        [BundleBlock("root", "recovery", payload)],
    )
    extent = result.blocks["root"]
    with pytest.raises(IntegrityError, match="streaming"):
        store.read_extent(result.relative_path, extent.offset, extent.length)
    chunks = list(
        store.iter_extent(result.relative_path, extent.offset, extent.length),
    )
    assert max(map(len, chunks)) <= 65536
    assert b"".join(chunks) == payload


def test_index_duplicate_keys_and_nonfinite_numbers_are_invalid(tmp_path):
    from swe.app.conversation_storage.bundle import HEADER, MAGIC, VERSION

    store = BundleStore(tmp_path)
    operation = new_operation_key()
    store.prepare_directory(operation)
    path = tmp_path / store.path_for(operation)
    table = b'{"x":["recovery",0,1],"x":["recovery",0,1]}'
    path.write_bytes(
        HEADER.pack(MAGIC, VERSION, 2, len(table), 1) + b"{}" + table + b"x",
    )
    with pytest.raises(IntegrityError):
        store.inspect(store.path_for(operation))
    meta = b'{"n":NaN}'
    table = b'{"x":["recovery",0,1]}'
    path.write_bytes(
        HEADER.pack(MAGIC, VERSION, len(meta), len(table), 1)
        + meta
        + table
        + b"x",
    )
    with pytest.raises(IntegrityError):
        store.inspect(store.path_for(operation))
