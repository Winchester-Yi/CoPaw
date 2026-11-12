# -*- coding: utf-8 -*-
"""Fresh, bounded immutable-body evidence for owner-task runtime readers."""

from .parallel import MigrationCoordinator
from .disk_manifest import DiskMap
from ..bundle import BundleStore, MAX_READ_BYTES, HEADER, MAX_INDEX_BYTES
from ..errors import IntegrityError


class PrefetchedBundles:
    """Strict read-only view: misses never fall back to unbounded owner IO."""

    def __init__(self, original):
        self.root = original.root
        self.indexes = {}
        self.blocks = {}

    validate_publication_locator = staticmethod(
        BundleStore.validate_publication_locator,
    )

    def inspect(self, relative_path, *, expected_length=None):
        if relative_path not in self.indexes:
            raise IntegrityError(
                "runtime publication is absent from fresh body evidence",
            )
        index = self.indexes[relative_path]
        if (
            expected_length is not None
            and expected_length != index.byte_length
        ):
            raise IntegrityError("prefetched publication length differs")
        return index

    def read_extent(
        self,
        relative_path,
        offset,
        length,
        *,
        expected_length=None,
    ):
        if (
            type(offset) is not int
            or type(length) is not int
            or offset < 0
            or length < 0
            or length > MAX_READ_BYTES
        ):
            raise IntegrityError("invalid prefetched extent")
        self.inspect(relative_path, expected_length=expected_length)
        for start, body in self.blocks[relative_path]:
            if start <= offset and offset + length <= start + len(body):
                return body[offset - start : offset - start + length]
        raise IntegrityError(
            "runtime locator is absent from fresh body evidence",
        )


def _inspect_bounded(original, path, byte_length, allowance):
    # Inspect's JSON decoder is bounded by format maxima, but reserve its input
    # expansion before invoking it. _open preserves BundleStore path safety.
    with original._open(path) as source:
        raw = source.read(HEADER.size)
    if len(raw) != HEADER.size:
        raise IntegrityError("truncated runtime bundle header")
    _magic, _version, meta_size, table_size, _payload = HEADER.unpack(raw)
    if meta_size > MAX_INDEX_BYTES or table_size > MAX_INDEX_BYTES:
        raise IntegrityError("runtime bundle index exceeds format limit")
    reservation = (meta_size + table_size) * 16 + 4096
    if reservation > allowance:
        raise ValueError("runtime index exceeds workspace memory allowance")
    return original.inspect(path, expected_length=byte_length), reservation


async def _prefetch(original, rows, io, allowance):
    cache = PrefetchedBundles(original)
    # Reserve SQL rows, parsed indexes and runtime JSON expansion headroom.
    # This is a conservative admission budget, not a process RSS guarantee.
    used = len(rows) * 16384
    for row in rows:
        path = original.validate_publication_locator(row)
        index, reservation = await io.call(
            _inspect_bounded,
            original,
            path,
            row["byte_length"],
            allowance - used,
        )
        used += reservation
        extents = list(index.blocks.values())
        size = sum(block.length * 16 + 512 for block in extents)
        if size + used > allowance or any(
            block.length > MAX_READ_BYTES for block in extents
        ):
            raise ValueError(
                "runtime body evidence exceeds workspace memory allowance",
            )
        used += size
        cache.indexes[path] = index
        cache.blocks[path] = []
        for block in extents:
            body = await io.call(
                original.read_extent,
                path,
                block.offset,
                block.length,
                expected_length=row["byte_length"],
            )
            cache.blocks[path].append((block.offset, body))
    return cache


async def verify_prefetched_workspaces(
    database,
    stores,
    inventory,
    runtime_check,
    *,
    io,
    workers,
    allowance,
    health_check=None,
    merge_result=None,
):
    from .readonly import ReadOnlyRetention
    from ..identity import WorkspaceScope

    # ReadOnlyRetention performs transient offline checks, not durable GC pins.
    # The migration offline fence remains responsible for stopping writers/GC.
    pins = {}
    coverage = _Coverage(len(inventory["workspaces"]))

    async def release(index):
        failure = None
        for retention, batch in pins.pop(index, []):
            try:
                await retention.finish_reader(batch)
            except BaseException as error:  # noqa: B036
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure

    async def locators(index):
        workspace = inventory["workspaces"][index]
        scope = tuple(value.encode() for value in workspace["scope"])
        rows = []
        after = None
        while True:
            page = await database.fetch_all(
                "SELECT id,scope_kind,service_unit_key,tenant_key,source_key,agent_key,"
                "operation_key,relative_path,byte_length FROM swe_conversation_publications "
                "WHERE scope_kind='workspace' AND service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s "
                "AND state='committed' AND gc_state='live' "
                + ("" if after is None else "AND id>%s ")
                + "ORDER BY id LIMIT 128",
                scope if after is None else (*scope, after),
            )
            if page:
                retention = ReadOnlyRetention(
                    database,
                    WorkspaceScope.from_values(workspace["scope"]),
                )
                batch = await retention.pin(
                    [row["id"] for row in page],
                    kind="reader",
                )
                pins.setdefault(index, []).append((retention, batch))
            rows.extend(page)
            if len(rows) * 16384 > allowance:
                raise ValueError(
                    "runtime locator evidence exceeds workspace memory allowance",
                )
            if len(page) < 128:
                return rows
            after = page[-1]["id"]

    async def runtime(index, cache):
        coverage.start(index)
        result = await runtime_check(inventory["workspaces"][index], cache)
        coverage.finish(index, result)

    async def worker(index, request):
        workspace = inventory["workspaces"][index]
        rows = await request("runtime_workspace_locators", index)
        cache = await _prefetch(
            stores[workspace["scope"][0]],
            rows,
            io,
            allowance,
        )
        await request("runtime_workspace_check", index, cache)
        await request("runtime_workspace_release", index)

    try:
        await MigrationCoordinator(
            {
                "runtime_workspace_locators": locators,
                "runtime_workspace_check": runtime,
                "runtime_workspace_release": release,
            },
            workers=workers,
            health_check=health_check,
        ).run(range(len(inventory["workspaces"])), worker)
        coverage.validate()
        if merge_result is not None:
            coverage.merge(merge_result)
    finally:
        try:
            await _release_all(pins, release)
        finally:
            coverage.close()


async def verify_prefetched_shares(
    database,
    stores,
    shares,
    runtime_check,
    *,
    io,
    workers,
    allowance,
    health_check=None,
    merge_result=None,
):
    from .readonly import ReadOnlyRetention
    from ..identity import WorkspaceScope

    # ReadOnlyRetention performs transient offline checks, not durable GC pins.
    # The migration offline fence remains responsible for stopping writers/GC.
    pins = {}
    coverage = _Coverage(len(shares))

    async def release(index):
        retained = pins.pop(index, None)
        if retained is not None:
            retention, batch = retained
            await retention.finish_reader(batch)

    async def locate(index):
        share = shares[index]
        record = await database.fetch_one(
            "SELECT publication_id FROM swe_chat_shares WHERE token=%s",
            (share["record"]["token"],),
        )
        if record is None:
            raise IntegrityError(
                "permanent share missing during runtime prefetch",
            )
        row = await database.fetch_one(
            "SELECT id,scope_kind,service_unit_key,tenant_key,source_key,agent_key,operation_key,relative_path,byte_length FROM swe_conversation_publications WHERE id=%s AND state='committed' AND gc_state='live'",
            (record["publication_id"],),
        )
        if (
            row is None
            or row["service_unit_key"] != share["service_unit_id"].encode()
        ):
            raise IntegrityError("runtime share publication ownership differs")
        retention = ReadOnlyRetention(
            database,
            WorkspaceScope.from_row(row),
            scope_kind=row["scope_kind"],
        )
        batch = await retention.pin([row["id"]], kind="reader")
        pins[index] = (retention, batch)
        return [row]

    async def runtime(index, cache):
        coverage.start(index)
        result = await runtime_check(shares[index], cache)
        coverage.finish(index, result)

    async def worker(index, request):
        rows = await request("runtime_share_locator", index)
        cache = await _prefetch(
            stores[shares[index]["service_unit_id"]],
            rows,
            io,
            allowance,
        )
        await request("runtime_share_check", index, cache)
        await request("runtime_share_release", index)

    try:
        await MigrationCoordinator(
            {
                "runtime_share_locator": locate,
                "runtime_share_check": runtime,
                "runtime_share_release": release,
            },
            workers=workers,
            health_check=health_check,
        ).run(range(len(shares)), worker)
        coverage.validate()
        if merge_result is not None:
            coverage.merge(merge_result)
    finally:
        try:
            await _release_all(pins, release)
        finally:
            coverage.close()


async def _release_all(pins, release):
    failure = None
    for index in tuple(pins):
        try:
            await release(index)
        except BaseException as error:  # noqa: B036
            if failure is None:
                failure = error
    if failure is not None:
        raise failure


class _Coverage:
    """Disk-backed exact runtime-check completion coverage."""

    def __init__(self, total):
        self.total = total
        self.completed = DiskMap()
        self.count = 0

    def start(self, index):
        if type(index) is not int or not 0 <= index < self.total:
            raise IntegrityError("runtime verification index is out of range")
        if str(index) in self.completed:
            raise IntegrityError("duplicate runtime verification index")

    def finish(self, index, result):
        self.completed[str(index)] = result
        self.count += 1

    def validate(self):
        if self.count != self.total:
            raise IntegrityError("runtime verification coverage is incomplete")

    def merge(self, merge_result):
        for index in range(self.total):
            merge_result(self.completed[str(index)])

    def close(self):
        self.completed.close()
