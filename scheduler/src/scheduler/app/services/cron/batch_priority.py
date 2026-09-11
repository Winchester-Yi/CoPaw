"""Persisted, batch-local priority ranks. Unconfigured entries sort last."""

from typing import Any, Mapping


def priority_key(row: Mapping[str, Any]) -> tuple[float, float]:
    payload = row.get("payload") or {}
    priority = (
        payload.get("dispatch_priority")
        if isinstance(payload, Mapping)
        else None
    )
    priority = priority if isinstance(priority, Mapping) else {}
    user, branch = (
        priority.get(name) for name in ("user_rank", "branch_rank")
    )
    return (
        user if type(user) is int and user > 0 else float("inf"),
        branch if type(branch) is int and branch > 0 else float("inf"),
    )


def priority_snapshot(
    policy: Mapping[str, Any], user: str, branch: str
) -> dict:
    policy = policy if isinstance(policy, Mapping) else {}
    users = policy.get("user_ids") or []
    branches = policy.get("branch_ids") or []
    users = users if isinstance(users, list) else []
    branches = branches if isinstance(branches, list) else []
    user_rank = users.index(user) + 1 if user in users else None
    branch_rank = branches.index(branch) + 1 if branch in branches else None
    return {
        "basis": (
            "user" if user_rank else "branch" if branch_rank else "default"
        ),
        "user_rank": user_rank,
        "branch_rank": branch_rank,
        "branch_id": branch,
    }
