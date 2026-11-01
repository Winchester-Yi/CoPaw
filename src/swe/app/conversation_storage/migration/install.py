# -*- coding: utf-8 -*-
"""Offline seven-table cutover with generated schemas and a durable DDL receipt."""

from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
import re
from uuid import UUID, uuid4

from mysql.connector.errors import Error as MySQLError

from ..backup import _atomic_json
from ..schema_contract import CHARSETS, COLUMNS, DEFAULTS, EXTRAS, INDEXES

TABLES = tuple(COLUMNS) + ("swe_chat_shares",)
SHARE = "swe_chat_shares"
SHARE_COLUMNS = (
    "token",
    "chat_id",
    "creator_id",
    "tenant_id",
    "snapshot_key",
    "created_at",
    "access_count",
    "last_access_at",
)
SQL_ROOT = Path(__file__).resolve().parents[5] / "scripts" / "sql"


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-zA-Z0-9_]{1,64}",
        value,
    ):
        raise ValueError("unsafe SQL schema/table identifier")
    return "`" + value + "`"


def _table(schema, name):
    if name not in TABLES and not re.fullmatch(
        r"swe_chat_shares_old_[a-f0-9]{32}",
        name,
    ):
        raise ValueError("table is outside the seven-table allowlist")
    return _identifier(schema) + "." + _identifier(name)


def check_verification(report, inventory):
    if not report.get("passed") or report.get("blockers"):
        raise ValueError("full migration verification has blockers")
    expected = [
        item["position"]
        for workspace in inventory["workspaces"]
        for kind in ("sessions", "chats")
        for item in workspace[kind]
    ]
    expected += [item["position"] for item in inventory["shares"]]
    expected += [
        "source-artifact:" + str(item["workspace"]) + "/" + item["path"]
        for item in inventory["files"]
    ]
    if sorted(report.get("checked", [])) != sorted(expected):
        raise ValueError("migration verification coverage is incomplete")


async def server_identity(database):
    row = await database.fetch_one(
        "SELECT @@server_uuid AS server_uuid, VERSION() AS version, DATABASE() AS schema_name",
    )
    version = re.match(r"(\d+)\.(\d+)\.(\d+)", row["version"])
    if (
        not version
        or tuple(map(int, version.groups())) < (8, 0, 21)
        or "mariadb" in row["version"].lower()
    ):
        raise ValueError(
            "atomic install requires reviewed MySQL 8.0.21+ compatibility",
        )
    return row


async def _names(database, schema):
    rows = await database.fetch_all(
        "SELECT TABLE_NAME,TABLE_TYPE,ENGINE,ROW_FORMAT FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
        (schema,),
    )
    return {row["TABLE_NAME"]: row for row in rows}


async def _columns(database, schema, table):
    return await database.fetch_all(
        "SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,EXTRA,GENERATION_EXPRESSION,CHARACTER_SET_NAME,COLLATION_NAME,ORDINAL_POSITION "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
        (schema, table),
    )


async def _indexes(database, schema, table):
    rows = await database.fetch_all(
        "SELECT INDEX_NAME,NON_UNIQUE,COLUMN_NAME,SEQ_IN_INDEX,SUB_PART,IS_VISIBLE FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY INDEX_NAME,SEQ_IN_INDEX",
        (schema, table),
    )
    result = {}
    for row in rows:
        if row["SUB_PART"] is not None or row["IS_VISIBLE"] != "YES":
            raise ValueError(
                "prefix/invisible indexes require separate schema review",
            )
        entry = result.setdefault(
            row["INDEX_NAME"],
            [int(row["NON_UNIQUE"]), []],
        )
        entry[1].append(row["COLUMN_NAME"])
    return {key: (value[0], tuple(value[1])) for key, value in result.items()}


def _encoding_matches(row, expected):
    charset, collation = expected
    actual = row["COLLATION_NAME"]
    return row["CHARACTER_SET_NAME"] == charset and (
        actual is None
        if collation is None
        else isinstance(actual, str) and fnmatchcase(actual, collation)
    )


async def _check_definitions(database, staging, application):
    for table, expected in COLUMNS.items():
        rows = await _columns(database, staging, table)
        actual = {
            row["COLUMN_NAME"]: (
                row["COLUMN_TYPE"].lower(),
                row["IS_NULLABLE"],
            )
            for row in rows
        }
        if actual != expected or any(
            row["GENERATION_EXPRESSION"] for row in rows
        ):
            raise ValueError(
                "staging table columns differ from reviewed contract: "
                + table,
            )
        if any(
            row["COLUMN_DEFAULT"] != DEFAULTS[table].get(row["COLUMN_NAME"])
            or row["EXTRA"] != EXTRAS[table].get(row["COLUMN_NAME"], "")
            for row in rows
        ):
            raise ValueError(
                "staging default/auto_increment differs from reviewed contract: "
                + table,
            )
        if any(
            not _encoding_matches(row, CHARSETS[table][row["COLUMN_NAME"]])
            for row in rows
        ):
            raise ValueError(
                "staging charset/collation differs from reviewed contract: "
                + table,
            )
        if await _indexes(database, staging, table) != INDEXES[table]:
            raise ValueError(
                "staging table indexes differ from reviewed contract: "
                + table,
            )
    original = await _columns(database, application, SHARE)
    imported = await _columns(database, staging, SHARE)
    if tuple(row["COLUMN_NAME"] for row in original) != SHARE_COLUMNS:
        raise ValueError(
            "original share requires reviewed eight-column legacy schema",
        )
    if [
        row for row in imported if row["COLUMN_NAME"] in SHARE_COLUMNS
    ] != original:
        raise ValueError(
            "staging share changed original eight-column definitions",
        )
    extra = {
        row["COLUMN_NAME"]: (
            row["COLUMN_TYPE"].lower(),
            row["IS_NULLABLE"],
            row["COLUMN_DEFAULT"],
            row["EXTRA"],
            row["GENERATION_EXPRESSION"],
            row["CHARACTER_SET_NAME"],
            row["COLLATION_NAME"],
        )
        for row in imported
        if row["COLUMN_NAME"] not in SHARE_COLUMNS
    }
    if extra != {
        key: ("bigint unsigned", "YES", None, "", "", None, None)
        for key in ("publication_id", "byte_offset", "byte_length")
    }:
        raise ValueError("staging share locator columns differ from contract")
    old_indexes = await _indexes(database, application, SHARE)
    if await _indexes(database, staging, SHARE) != {
        **old_indexes,
        "ix_swe_chat_shares_publication": (1, ("publication_id",)),
    }:
        raise ValueError("staging share changed original indexes")


async def _check_share_rows(database, staging, application):
    """Preserve every original token, counter and timestamp, including orphans."""
    source, destination = _table(application, SHARE), _table(staging, SHARE)
    old_count = await database.fetch_one("SELECT COUNT(*) AS n FROM " + source)
    new_count = await database.fetch_one(
        "SELECT COUNT(*) AS n FROM " + destination,
    )
    if old_count != new_count:
        raise ValueError(
            "original share rows differ from staging; fixed boundary is stale or incomplete",
        )
    columns = ",".join(_identifier(name) for name in SHARE_COLUMNS)
    after = None
    while True:
        where = " WHERE token>%s" if after is not None else ""
        rows = await database.fetch_all(
            "SELECT "
            + columns
            + " FROM "
            + source
            + where
            + " ORDER BY token LIMIT 256",
            (after,) if after is not None else None,
        )
        if not rows:
            return
        imported = await database.fetch_all(
            "SELECT "
            + columns
            + " FROM "
            + destination
            + " WHERE token IN ("
            + ",".join(["%s"] * len(rows))
            + ") ORDER BY token",
            tuple(row["token"] for row in rows),
        )
        if rows != imported:
            raise ValueError(
                "original share rows differ from staging; fixed boundary is stale or incomplete",
            )
        after = rows[-1]["token"]


async def _check_dependencies(database, schemas):
    rows = await database.fetch_all(
        "SELECT TRIGGER_SCHEMA,EVENT_OBJECT_TABLE FROM information_schema.TRIGGERS WHERE EVENT_OBJECT_SCHEMA IN (%s,%s)",
        schemas,
    )
    if any(row["EVENT_OBJECT_TABLE"] in TABLES for row in rows):
        raise ValueError(
            "triggers on cutover tables prohibit cross-schema rename",
        )
    rows = await database.fetch_all(
        "SELECT TABLE_SCHEMA,TABLE_NAME,REFERENCED_TABLE_SCHEMA,REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE REFERENCED_TABLE_NAME IS NOT NULL AND (TABLE_SCHEMA IN (%s,%s) OR REFERENCED_TABLE_SCHEMA IN (%s,%s))",
        (*schemas, *schemas),
    )
    if any(
        row["TABLE_NAME"] in TABLES or row["REFERENCED_TABLE_NAME"] in TABLES
        for row in rows
    ):
        raise ValueError(
            "foreign-key dependencies on cutover tables require review",
        )
    # Views outside the application schema can refer into it too.
    rows = await database.fetch_all(
        "SELECT VIEW_SCHEMA,VIEW_NAME,TABLE_SCHEMA,TABLE_NAME FROM information_schema.VIEW_TABLE_USAGE WHERE TABLE_SCHEMA IN (%s,%s)",
        schemas,
    )
    if any(row["TABLE_NAME"] in TABLES for row in rows):
        raise ValueError("view dependencies on cutover tables require review")


async def _check_privileges(database, schemas):
    rows = await database.fetch_all("SHOW GRANTS FOR CURRENT_USER")
    grants = [str(next(iter(row.values()))).replace("`", "") for row in rows]
    required = {
        "CREATE",
        "ALTER",
        "DROP",
        "INSERT",
        "SELECT",
        "UPDATE",
        "DELETE",
    }
    for schema in schemas:
        effective = set()
        for grant in grants:
            match = re.match(r"GRANT (.+) ON ([^ ]+) TO ", grant)
            if match and match[2] in ("*.*", schema + ".*"):
                effective.update(
                    (
                        required
                        if match[1] == "ALL PRIVILEGES"
                        else match[1].split(", ")
                    ),
                )
        if not required <= effective:
            raise ValueError(
                "cutover requires explicit schema/global DDL and data privileges; role-only grants require separate review",
            )


async def _check_mdl(database, schemas):
    instrument = await database.fetch_one(
        "SELECT ENABLED FROM performance_schema.setup_instruments WHERE NAME='wait/lock/metadata/sql/mdl'",
    )
    if not instrument or instrument["ENABLED"] != "YES":
        raise ValueError(
            "metadata-lock instrumentation must be enabled for cutover",
        )
    locks = await database.fetch_all(
        "SELECT OBJECT_SCHEMA,OBJECT_NAME,LOCK_STATUS FROM performance_schema.metadata_locks WHERE OBJECT_SCHEMA IN (%s,%s) AND OBJECT_TYPE='TABLE' AND OWNER_THREAD_ID<>(SELECT THREAD_ID FROM performance_schema.threads WHERE PROCESSLIST_ID=CONNECTION_ID())",
        schemas,
    )
    if any(row["OBJECT_NAME"] in TABLES for row in locks):
        raise ValueError(
            "cutover tables have active/pending metadata locks; drain writers/readers",
        )


async def _apply_script(database, path):
    # Only reviewed checked-in scripts, never a manifest-supplied SQL path.
    source = "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )
    async with database.acquire() as session:
        for statement in source.split(";"):
            if statement.strip():
                await session.execute(statement.strip())


class ConversationInstaller:
    def __init__(self, database, manifest, target):
        generated = "conversation_staging_" + UUID(manifest.migration_id).hex
        if (
            target.schema != generated
            or target.migration_id != manifest.migration_id
        ):
            raise ValueError(
                "installer requires the job's generated staging schema",
            )
        _identifier(target.application_schema)
        self.db, self.manifest, self.target = database, manifest, target
        self.old_share = (
            "swe_chat_shares_old_" + UUID(manifest.migration_id).hex
        )
        self.receipt_path = manifest.path.with_suffix(
            manifest.path.suffix + ".install.json",
        )

    def rename_sql(self, *, reverse=False):
        staging, app = self.target.schema, self.target.application_schema
        pairs = [(_table(app, SHARE), _table(staging, self.old_share))]
        pairs += [
            (_table(staging, table), _table(app, table)) for table in TABLES
        ]
        if reverse:
            pairs = [(target, source) for source, target in reversed(pairs)]
        return "RENAME TABLE " + ", ".join(
            source + " TO " + target for source, target in pairs
        )

    async def check_server(self, staging, identity):
        actual = await server_identity(staging)
        if (
            actual["server_uuid"] != identity["server_uuid"]
            or actual["version"] != identity["version"]
        ):
            raise ValueError(
                "staging must be on the same MySQL instance/version",
            )
        if actual["schema_name"] != self.target.schema:
            raise ValueError("staging connection selected the wrong schema")

    async def prepare_staging(self):
        """Create only this job's staging schema, retaining original share DDL."""
        from swe.database.connection import DatabaseConnection

        binding = {
            "schema": self.target.schema,
            "nas_root": self.manifest.data["offline"]["nas_root"],
            "migration_id": self.manifest.migration_id,
        }
        names = await _names(self.db, self.target.schema)
        if names and self.manifest.data.get("target") != binding:
            raise ValueError(
                "unowned existing staging schema cannot be reused",
            )
        if set(names) - set(TABLES):
            raise ValueError("staging contains unexpected objects")
        schema = await self.db.fetch_one(
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
            (self.target.schema,),
        )
        if schema and self.manifest.data.get("target") != binding:
            raise ValueError(
                "unowned existing staging schema cannot be reused",
            )
        if schema is None:
            await self.db.execute(
                "CREATE DATABASE "
                + _identifier(self.target.schema)
                + " CHARACTER SET utf8mb4",
            )
        self.manifest.bind_target(binding)
        staging = DatabaseConnection(
            self.db.config.model_copy(update={"database": self.target.schema}),
        )
        try:
            await staging.connect()
            # DDL is resumable table-by-table without IF NOT EXISTS masking drift.
            source = "\n".join(
                line
                for line in (SQL_ROOT / "conversation_metadata_tables.sql")
                .read_text()
                .splitlines()
                if not line.lstrip().startswith("--")
            )
            for statement in source.split(";"):
                statement = statement.strip()
                if not statement:
                    continue
                match = re.match(
                    r"CREATE TABLE (swe_conversation_[a-z]+) ",
                    statement,
                )
                if not match or match[1] not in COLUMNS:
                    raise ValueError("unexpected checked-in conversation DDL")
                if match[1] not in names:
                    await staging.execute(statement)
            if SHARE not in names:
                await self.db.execute(
                    "CREATE TABLE "
                    + _table(self.target.schema, SHARE)
                    + " LIKE "
                    + _table(self.target.application_schema, SHARE),
                )
            columns = await _columns(staging, self.target.schema, SHARE)
            if len(columns) == 8:
                await _apply_script(
                    staging,
                    SQL_ROOT / "conversation_share_publication_extension.sql",
                )
            await _check_definitions(
                self.db,
                self.target.schema,
                self.target.application_schema,
            )
        finally:
            await staging.close()

    async def preflight(self, staging):
        identity = await server_identity(self.db)
        if identity["schema_name"] != self.target.application_schema:
            raise ValueError(
                "control connection must select the application schema",
            )
        await self.check_server(staging, identity)
        source = await _names(self.db, self.target.schema)
        destination = await _names(self.db, self.target.application_schema)
        if set(source) != set(TABLES):
            raise ValueError(
                "staging must contain exactly the seven cutover tables",
            )
        if SHARE not in destination or any(
            table in destination for table in COLUMNS
        ):
            raise ValueError(
                "original share must exist; existing six new tables prohibit install",
            )
        if any(
            row["TABLE_TYPE"] != "BASE TABLE" or row["ENGINE"] != "InnoDB"
            for row in [*source.values(), destination[SHARE]]
        ):
            raise ValueError("cutover requires seven InnoDB base tables")
        if any(source[table]["ROW_FORMAT"] != "Dynamic" for table in COLUMNS):
            raise ValueError("conversation tables require DYNAMIC row format")
        await _check_definitions(
            self.db,
            self.target.schema,
            self.target.application_schema,
        )
        await _check_share_rows(
            self.db,
            self.target.schema,
            self.target.application_schema,
        )
        schemas = (self.target.schema, self.target.application_schema)
        await _check_dependencies(self.db, schemas)
        await _check_privileges(self.db, schemas)
        await _check_mdl(self.db, schemas)
        return {
            "server": identity,
            "staging_tables": sorted(source),
            "application_tables": sorted(
                table for table in destination if table in TABLES
            ),
        }

    async def install(self, staging, bundles, inventory):
        from .verify import verify_import

        report = await verify_import(
            staging,
            bundles,
            self.manifest,
            inventory,
            self.target,
        )
        check_verification(report, inventory)
        if self.receipt_path.exists():
            raise ValueError(
                "install receipt already exists; inspect table states before retry",
            )
        # Expected SQL failure in the probe discards its connection. Run it
        # before pinning the named-lock connection used by the real cutover.
        probe = await probe_atomic_rename(self.db)
        async with self.db.named_lock(
            "conversation-install:" + self.target.application_schema,
            timeout=0,
        ):
            before = await self.preflight(staging)
            if probe["server_uuid"] != before["server"]["server_uuid"]:
                raise ValueError(
                    "atomic capability probe ran on a different server",
                )
            receipt = {
                "version": 1,
                "migration_id": self.manifest.migration_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "phase": "rename_pending",
                "before": before,
                "verification": report,
                "atomic_probe": probe,
                "rename_sql": self.rename_sql(),
                "reverse_sql_before_any_startup_or_write": self.rename_sql(
                    reverse=True,
                ),
                "rollback_boundary": "Any initialization, smoke test, share access or new write requires complete reverse export; never execute reverse SQL after startup.",
            }
            _atomic_json(self.receipt_path, receipt)
            # Bound MDL acquisition on the same connection as the atomic DDL.
            try:
                async with self.db.acquire() as session:
                    await session.execute("SET SESSION lock_wait_timeout=5")
                    await _check_mdl(
                        self.db,
                        (self.target.schema, self.target.application_schema),
                    )
                    await session.execute(self.rename_sql())
            except Exception:
                # A lost reply cannot prove whether MySQL committed DDL. Keep
                # the durable pending receipt and never retry automatically.
                raise RuntimeError(
                    "rename outcome is unknown; keep all writers stopped and inspect the durable install receipt and exact table names",
                ) from None
            after = {
                "staging_tables": sorted(
                    await _names(self.db, self.target.schema),
                ),
                "application_tables": sorted(
                    table
                    for table in await _names(
                        self.db,
                        self.target.application_schema,
                    )
                    if table in TABLES
                ),
            }
            if after != {
                "staging_tables": [self.old_share],
                "application_tables": sorted(TABLES),
            }:
                raise RuntimeError(
                    "post-rename state is unknown; keep all writers stopped and inspect install receipt",
                )
            receipt.update(phase="installed_before_startup", after=after)
            _atomic_json(self.receipt_path, receipt)
            return receipt


async def probe_atomic_rename(database):
    """Reproduce failed and successful seven-table rename in disposable schemas."""
    identity = await server_identity(database)
    suffix = uuid4().hex
    source, target = (
        "conversation_probe_" + suffix,
        "conversation_probe_dst_" + suffix,
    )
    created = []
    try:
        for schema in (source, target):
            await database.execute("CREATE DATABASE " + _identifier(schema))
            created.append(schema)
        for table in TABLES:
            await database.execute(
                "CREATE TABLE "
                + _table(source, table)
                + " (marker INT) ENGINE=InnoDB",
            )
        await database.execute(
            "CREATE TABLE "
            + _table(target, SHARE)
            + " (marker INT) ENGINE=InnoDB",
        )
        old = "swe_chat_shares_old_" + suffix
        pairs = [(_table(target, SHARE), _table(source, old))] + [
            (_table(source, table), _table(target, table)) for table in TABLES
        ]
        valid = "RENAME TABLE " + ", ".join(a + " TO " + b for a, b in pairs)
        # Deliberately conflict at the final destination. Atomic failure must
        # restore the original share and all seven source names.
        await database.execute(
            "CREATE TABLE "
            + _table(target, TABLES[-2])
            + " (marker INT) ENGINE=InnoDB",
        )
        try:
            await database.execute(valid)
        except MySQLError:
            pass
        else:
            raise ValueError(
                "atomic rename probe did not reject conflicting destination",
            )
        if set(await _names(database, source)) != set(TABLES) or set(
            await _names(database, target),
        ) != {SHARE, TABLES[-2]}:
            raise ValueError("failed multi-table rename was not atomic")
        await database.execute("DROP TABLE " + _table(target, TABLES[-2]))
        await database.execute(valid)
        if set(await _names(database, source)) != {old} or set(
            await _names(database, target),
        ) != set(TABLES):
            raise ValueError("successful multi-table rename was incomplete")
        return {
            "server_uuid": identity["server_uuid"],
            "version": identity["version"],
            "source_schema": source,
            "target_schema": target,
            "failure_atomic": True,
            "success_atomic": True,
            "probe_id": suffix,
        }
    finally:
        await _cleanup_probe_schemas(database, created)


async def _cleanup_probe_schemas(database, schemas):
    failed = []
    for schema in reversed(schemas):
        if not re.fullmatch(
            r"conversation_probe(?:_dst)?_[a-f0-9]{32}",
            schema,
        ):
            raise ValueError("probe cleanup refuses a non-generated schema")
        try:
            await database.execute("DROP DATABASE " + _identifier(schema))
        except Exception:
            failed.append(schema)
    if failed:
        raise RuntimeError(
            "atomic probe cleanup requires inspection of generated schemas: "
            + ", ".join(failed),
        )
