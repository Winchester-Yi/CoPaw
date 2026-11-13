# -*- coding: utf-8 -*-
"""Locked tenant publication state; reports never grant runtime admission."""

import json
from uuid import UUID

from ..identity import service_unit_bytes

TENANTS = "swe_conversation_migration_tenants"
TRANSITIONS = {
    "discovered": {"importing", "failed"},
    "importing": {"imported", "failed"},
    "imported": {"importing", "verified", "failed"},
    "verified": {"importing", "published", "failed"},
    "failed": {"importing", "failed"},
    "published": set(),
}


def _json(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


class TenantState:
    def __init__(self, database, migration_id):
        if str(UUID(migration_id)) != migration_id:
            raise ValueError("invalid tenant migration identity")
        self.db = database
        self.migration_id = migration_id.encode()

    def _keys(self, service, tenant):
        service_unit_bytes(service)
        if not isinstance(tenant, str) or len(tenant.encode()) > 256:
            raise ValueError("invalid migration tenant identity")
        return self.migration_id, service.encode(), tenant.encode()

    async def get(self, service, tenant, *, locked=False):
        return await self.db.fetch_one(
            "SELECT * FROM "
            + TENANTS
            + " WHERE migration_id=%s AND service_unit_key=%s AND tenant_key=%s"
            + (" FOR UPDATE" if locked else ""),
            self._keys(service, tenant),
        )

    async def register(self, tenants):
        """Register exact frozen evidence; never replace existing evidence."""
        async with self.db.transaction():
            for item in tenants:
                service, tenant = item["service_unit_id"], item["tenant_id"]
                evidence = _json(item.get("evidence", item))
                current = await self.get(service, tenant, locked=True)
                if current is not None:
                    if current["evidence_text"] != evidence:
                        raise ValueError("tenant frozen evidence changed")
                    continue
                await self.db.execute(
                    "INSERT INTO "
                    + TENANTS
                    + " (migration_id,service_unit_key,tenant_key,state,evidence_text,verification_text,failure_text,attempts,updated_at,published_at)"
                    + " VALUES (%s,%s,%s,'discovered',%s,'{}',NULL,0,UTC_TIMESTAMP(6),NULL)",
                    (*self._keys(service, tenant), evidence),
                )

    async def transition(self, service, tenant, state, verification=None):
        if state == "published":
            from .authority import validate_receipt

            proof = validate_receipt(verification)
            entry = proof["services"].get(service)
            if (
                proof["migration_id"].encode() != self.migration_id
                or set(proof["services"]) != {service}
                or entry is None
                or any(
                    item["scope"][1] != tenant for item in entry["workspaces"]
                )
            ):
                raise ValueError("tenant publication proof scope differs")
        if state not in TRANSITIONS:
            raise ValueError("invalid migration tenant state")
        async with self.db.transaction():
            current = await self.get(service, tenant, locked=True)
            if current is None:
                raise ValueError("migration tenant is not registered")
            if state == "published":
                self._validate_evidence(current, service, tenant, proof)
            previous = current["state"]
            if previous == "published":
                if state != "published" or (
                    verification is not None
                    and current["verification_text"] != _json(verification)
                ):
                    raise ValueError("published tenant cannot be changed")
                return current
            if state != previous and state not in TRANSITIONS[previous]:
                raise ValueError("invalid migration tenant transition")
            if state == "published" and verification is None:
                raise ValueError("tenant publication requires verification")
            changed = await self.db.execute(
                "UPDATE "
                + TENANTS
                + " SET state=%s,verification_text=COALESCE(%s,verification_text),"
                + "failure_text=NULL,attempts=attempts+%s,updated_at=UTC_TIMESTAMP(6),"
                + "published_at=CASE WHEN %s='published' THEN UTC_TIMESTAMP(6) ELSE published_at END"
                + " WHERE migration_id=%s AND service_unit_key=%s AND tenant_key=%s AND state=%s",
                (
                    state,
                    None if verification is None else _json(verification),
                    int(state == "importing" and previous != "importing"),
                    state,
                    *self._keys(service, tenant),
                    previous,
                ),
            )

            if changed != 1:
                raise ValueError(
                    "tenant migration state compare-and-set failed",
                )

    @staticmethod
    def _validate_evidence(current, service, tenant, proof):
        evidence = json.loads(current["evidence_text"])
        entry = proof["services"][service]
        try:
            expected = [
                {
                    "root": item["runtime_root"],
                    "scope": [
                        item[key]
                        for key in (
                            "service_unit_id",
                            "tenant_id",
                            "source_id",
                            "agent_id",
                        )
                    ],
                }
                for item in evidence["workspaces"]
            ]
            if (
                evidence["service"]["service_unit_id"] != service
                or evidence["service"]["nas_root"] != entry["nas_root"]
                or any(
                    item["scope"][:2] != [service, tenant] for item in expected
                )
                or sorted(expected, key=_json)
                != sorted(entry["workspaces"], key=_json)
            ):
                raise ValueError("tenant publication evidence scope differs")
        except (KeyError, TypeError) as error:
            raise ValueError(
                "tenant publication evidence is incomplete",
            ) from error

    async def fail(self, service, tenant, stage, code, reason):
        # Caller supplies public-safe reason; no raw exceptions or tokens here.
        async with self.db.transaction():
            current = await self.get(service, tenant, locked=True)
            if current is None or current["state"] == "published":
                raise ValueError("published or unknown tenant cannot fail")
            changed = await self.db.execute(
                "UPDATE "
                + TENANTS
                + " SET state='failed',failure_text=%s,updated_at=UTC_TIMESTAMP(6)"
                + " WHERE migration_id=%s AND service_unit_key=%s AND tenant_key=%s AND state=%s",
                (
                    _json({"stage": stage, "code": code, "reason": reason}),
                    *self._keys(service, tenant),
                    current["state"],
                ),
            )

            if changed != 1:
                raise ValueError("tenant failure state compare-and-set failed")

    async def publish(self, service, tenant, verification):
        return await self.transition(
            service,
            tenant,
            "published",
            verification,
        )
