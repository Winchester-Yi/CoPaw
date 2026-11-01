# -*- coding: utf-8 -*-
"""One SQL authority injected into an existing workspace and NAS mount."""

from pathlib import Path
import stat

from .archive_runtime import MySQLArchiveStore
from .bundle import BundleStore
from .chat_repository import MySQLChatRepository
from .execution import ExecutionStore
from .history import HistoryStore
from .identity import WorkspaceScope
from .publications import PublicationStore
from .session_adapter import MySQLSessionAdapter
from .shares import ShareStorage
from .schema_contract import COLUMNS, INDEXES, DEFAULTS, EXTRAS

TABLES = tuple(
    "swe_conversation_" + suffix
    for suffix in (
        "sessions",
        "chats",
        "turns",
        "publications",
        "messages",
        "retentions",
    )
)


class WorkspaceConversationStorage:
    """Build adapters only; never bootstrap another workspace or run DDL."""

    authority = "mysql"

    def __init__(
        self,
        database,
        scope,
        workspace_root,
        *,
        cursor_secret,
        nas_root=None,
    ):
        if not isinstance(scope, WorkspaceScope):
            raise TypeError(
                "conversation storage requires a trusted workspace scope",
            )
        root = Path(workspace_root).absolute()
        try:
            mode = root.lstat().st_mode
        except FileNotFoundError as error:
            raise ValueError(
                "conversation storage requires an existing workspace",
            ) from error
        if stat.S_ISLNK(mode):
            raise ValueError("conversation workspace root cannot be a symlink")
        if not stat.S_ISDIR(mode):
            raise ValueError(
                "conversation storage requires an existing workspace directory",
            )
        self.workspace_root = root
        self.db, self.scope = database, scope
        self.bundles = BundleStore(
            root if nas_root is None else existing_root(nas_root),
        )
        self.publications = PublicationStore(database, self.bundles)
        self.executions = ExecutionStore(
            database,
            publications=self.publications,
        )
        self.chats = MySQLChatRepository(database, scope, self.bundles)
        self.history = HistoryStore(
            database,
            scope,
            self.bundles,
            cursor_secret=cursor_secret,
        )
        self.shares = ShareStorage(database, self.bundles, scope=scope)
        self._sessions = {}
        self.archive = MySQLArchiveStore(
            database,
            scope,
            self.bundles,
            self.session_for("console"),
        )

    def session_for(self, channel):
        if channel not in self._sessions:
            self._sessions[channel] = MySQLSessionAdapter(
                self.db,
                self.scope,
                self.bundles,
                channel=channel,
            )
        return self._sessions[channel]

    async def bind_workspace(self, workspace):
        """Attach storage to already resolved services and the original root."""
        from ..runner.manager import ChatManager

        if Path(workspace.workspace_dir).absolute() != self.workspace_root:
            raise ValueError("conversation workspace root changed")
        await self.verify_schema()
        services = workspace._service_manager.services
        manager = ChatManager(
            repo=self.chats,
            archive_store=self.archive,
            resource_root=self.workspace_root / ".scenario_sessions",
            expert_dependency_root=self.workspace_root,
        )
        services["chat_manager"] = manager
        workspace.conversation_storage = self
        runner = services["runner"]
        runner.session = self.session_for("console")
        runner.set_chat_manager(manager)
        if hasattr(runner, "set_workspace"):
            runner.set_workspace(workspace)
        memory = services.get("memory_manager")
        if memory is not None:
            memory.bind_conversation_archive(self.archive)
        return manager

    async def verify_schema(self):
        """Readiness validates the installed schema without creating objects."""
        rows = await self.db.fetch_all(
            "SELECT TABLE_NAME,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,EXTRA FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ("
            + ",".join(["%s"] * len(TABLES))
            + ")",
            TABLES,
        )
        columns = {}
        for row in rows:
            table, name = row["TABLE_NAME"], row["COLUMN_NAME"]
            if table not in COLUMNS or name not in COLUMNS[table]:
                raise RuntimeError(
                    "Conversation metadata schema has an unexpected column",
                )
            if row["COLUMN_DEFAULT"] != DEFAULTS[table].get(name) or row[
                "EXTRA"
            ].lower() != EXTRAS[table].get(name, ""):
                raise RuntimeError(
                    "Conversation metadata schema defaults or generated identity differ",
                )
            columns.setdefault(row["TABLE_NAME"], {})[row["COLUMN_NAME"]] = (
                row["COLUMN_TYPE"].lower(),
                row["IS_NULLABLE"],
            )
        if columns != COLUMNS:
            raise RuntimeError("Conversation metadata schema is incompatible")
        rows = await self.db.fetch_all(
            "SELECT TABLE_NAME,INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME,SUB_PART "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME IN (" + ",".join(["%s"] * len(TABLES)) + ") "
            "ORDER BY TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX",
            TABLES,
        )
        indexes = {}
        for row in rows:
            if row["SUB_PART"] is not None:
                raise RuntimeError(
                    "Conversation metadata schema has a prefix index",
                )
            table = indexes.setdefault(row["TABLE_NAME"], {})
            entry = table.setdefault(
                row["INDEX_NAME"],
                (int(row["NON_UNIQUE"]), []),
            )
            entry[1].append(row["COLUMN_NAME"])
        actual = {
            table: {
                name: (unique, tuple(names))
                for name, (unique, names) in fields.items()
            }
            for table, fields in indexes.items()
        }
        if actual != INDEXES:
            raise RuntimeError(
                "Conversation metadata schema indexes are incompatible",
            )
        names = (*TABLES, "swe_chat_shares")
        tables = await self.db.fetch_all(
            "SELECT TABLE_NAME,ENGINE,TABLE_TYPE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ("
            + ",".join(["%s"] * len(names))
            + ")",
            names,
        )
        if {row["TABLE_NAME"] for row in tables} != set(names) or any(
            row["ENGINE"] != "InnoDB" or row["TABLE_TYPE"] != "BASE TABLE"
            for row in tables
        ):
            raise RuntimeError(
                "Conversation metadata schema requires InnoDB base tables",
            )
        await self.shares.store.verify_locator_schema()


def existing_root(path):
    root = Path(path).expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError(
            "conversation storage requires an existing nonsymlink root",
        )
    return root
