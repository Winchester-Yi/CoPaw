# -*- coding: utf-8 -*-
"""Database-backed tenant publication admission; no negative cache or fallback."""

from contextlib import asynccontextmanager
import json
from uuid import UUID

from .errors import IntegrityError, StorageUnavailable
from .migration.authority import validate_receipt


class TenantNotPublished(StorageUnavailable):
    code = "conversation_tenant_not_published"

    def __init__(self):
        super().__init__("Conversation tenant is under maintenance")


class TenantAdmission:
    def __init__(self, database, document):
        if (
            set(document)
            != {
                "version",
                "service_unit_id",
                "nas_root",
                "migration_id",
            }
            or document["version"] != 3
        ):
            raise IntegrityError("invalid partial migration runtime document")
        if str(UUID(document["migration_id"])) != document["migration_id"]:
            raise IntegrityError("invalid partial migration identity")
        self.db = database
        self.document = dict(document)

    async def validate(self):
        doc = self.document
        job = await self.db.fetch_one(
            "SELECT target_binding_text,source_binding_text FROM "
            "swe_conversation_migration_jobs WHERE migration_id=%s",
            (doc["migration_id"].encode(),),
        )
        if job is None:
            raise IntegrityError("partial migration job is missing")
        binding = json.loads(job["target_binding_text"])
        source = json.loads(job["source_binding_text"])
        identity = await self.db.fetch_one(
            "SELECT @@server_uuid AS server_uuid,DATABASE() AS schema_name",
        )
        if (
            source.get("tenant_isolation") is not True
            or binding.get("server_uuid") != identity["server_uuid"]
            or binding.get("schema") != identity["schema_name"]
            or binding.get("migration_id") != doc["migration_id"]
            or binding.get("nas_roots", {}).get(doc["service_unit_id"])
            != doc["nas_root"]
        ):
            raise IntegrityError("partial migration authority binding differs")

    async def require(self, tenant_id):
        await self.validate()
        doc = self.document
        row = await self.db.fetch_one(
            "SELECT state,verification_text FROM swe_conversation_migration_tenants "
            "WHERE migration_id=%s AND service_unit_key=%s AND tenant_key=%s",
            (
                doc["migration_id"].encode(),
                doc["service_unit_id"].encode(),
                tenant_id.encode(),
            ),
        )
        if row is None or row["state"] != "published":
            raise TenantNotPublished()
        proof = validate_receipt(json.loads(row["verification_text"]))
        service = proof["services"].get(doc["service_unit_id"])
        if (
            proof["migration_id"] != doc["migration_id"]
            or set(proof["services"]) != {doc["service_unit_id"]}
            or service is None
            or service["nas_root"] != doc["nas_root"]
            or any(
                item["scope"][1] != tenant_id for item in service["workspaces"]
            )
        ):
            raise IntegrityError("tenant publication proof differs")
        return service["workspaces"]


class TenantDatabase:
    """Protect even previously cached adapters before each database operation."""

    def __init__(self, database, admission, tenant_id):
        self.database, self.admission, self.tenant_id = (
            database,
            admission,
            tenant_id,
        )

    def __getattr__(self, name):
        member = getattr(self.database, name)
        if name not in {
            "fetch_one",
            "fetch_all",
            "execute",
            "insert",
            "executemany",
            "execute_many",
            "execute_result",
            "insert_prepared",
        }:
            return member

        async def guarded(*args, **kwargs):
            await self.admission.require(self.tenant_id)
            return await member(*args, **kwargs)

        return guarded

    @asynccontextmanager
    async def acquire(self, *args, **kwargs):
        await self.admission.require(self.tenant_id)
        async with self.database.acquire(*args, **kwargs) as session:
            yield TenantDatabase(session, self.admission, self.tenant_id)

    @asynccontextmanager
    async def transaction(self, *args, **kwargs):
        await self.admission.require(self.tenant_id)
        async with self.database.transaction(*args, **kwargs):
            yield self


async def partial_jobs(database, service_unit_id=None):
    """Detect opt-in jobs without requiring the new table on legacy installs."""
    exists = await database.fetch_one(
        "SELECT COUNT(*) AS tenant_controls FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME="
        "'swe_conversation_migration_tenants'",
    )
    if not exists or not int(exists.get("tenant_controls") or 0):
        return []
    rows = await database.fetch_all(
        "SELECT migration_id,target_binding_text,source_binding_text "
        "FROM swe_conversation_migration_jobs",
    )
    result = []
    for row in rows:
        source = json.loads(row["source_binding_text"])
        if source.get("tenant_isolation") is not True:
            continue
        binding = json.loads(row["target_binding_text"])
        if service_unit_id is None or service_unit_id in binding.get(
            "nas_roots",
            {},
        ):
            result.append((row, binding))
    return result


async def partial_admission(database, scope):
    jobs = await partial_jobs(database, scope.service_unit_id)
    if len(jobs) > 1:
        raise IntegrityError("ambiguous partial migration authority")
    if not jobs:
        return None
    row, binding = jobs[0]
    migration_id = row["migration_id"]
    if isinstance(migration_id, bytes):
        migration_id = migration_id.decode()
    return TenantAdmission(
        database,
        {
            "version": 3,
            "service_unit_id": scope.service_unit_id,
            "nas_root": binding["nas_roots"][scope.service_unit_id],
            "migration_id": migration_id,
        },
    )


async def tenant_maintenance_response(_request, _error):
    """One safe response for failures from endpoints and outer middleware."""
    from starlette.responses import JSONResponse

    return JSONResponse(
        status_code=503,
        content={
            "detail": "Conversation tenant is under maintenance",
            "code": TenantNotPublished.code,
        },
        headers={"Retry-After": "30"},
    )
