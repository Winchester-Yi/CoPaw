# -*- coding: utf-8 -*-
"""CLI commands for environment variable management."""

from __future__ import annotations

import os
from urllib.parse import quote

import click
import httpx

from ..envs import load_envs, set_env_var
from .http import client, resolve_base_url

MASKED_ENV_VALUE = "********"


@click.group("env")
def env_group() -> None:
    """Manage environment variables."""


def _request(
    ctx: click.Context,
    method: str,
    path: str,
    headers: dict[str, str],
    **kwargs,
):
    """Call the local environment API and normalize CLI errors."""
    try:
        with client(resolve_base_url(ctx, None)) as api:
            response = getattr(api, method)(
                path,
                headers=headers,
                **kwargs,
            )
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"Environment API request failed: {exc}",
        ) from exc

    if response.status_code >= 400:
        detail = None
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            detail = payload.get("detail")
        message = detail or (
            f"Environment API request failed (HTTP {response.status_code})"
        )
        raise click.ClickException(str(message))
    return response


def _scope_headers() -> dict[str, str]:
    """Build scope headers from trusted shell runtime claims.

    Shell commands receive these values from the current request headers via
    ``apply_runtime_claim_env``.  They are intentionally not CLI options so a
    caller cannot override the request scope from the command line.
    """
    tenant_id = os.environ.get("SWE_TENANT_ID", "").strip()
    source_id = os.environ.get("SWE_SOURCE_ID", "").strip()
    missing = [
        name
        for name, value in (
            ("SWE_TENANT_ID", tenant_id),
            ("SWE_SOURCE_ID", source_id),
        )
        if not value
    ]
    if missing:
        raise click.ClickException(
            "Runtime scope claims are required: " + ", ".join(missing),
        )
    return {
        "X-Tenant-Id": tenant_id,
        "X-Source-Id": source_id,
    }


def _display_value(value: str, show_values: bool) -> str:
    """Mask non-empty environment values unless explicitly requested."""
    if show_values or not value:
        return value
    return MASKED_ENV_VALUE


# ---------------------------------------------------------------
# list
# ---------------------------------------------------------------


@env_group.command("list")
@click.option(
    "--show-values",
    is_flag=True,
    help="Show environment values instead of masking them.",
)
@click.pass_context
def list_cmd(
    ctx: click.Context,
    show_values: bool,
) -> None:
    """List all environment variables."""
    response = _request(
        ctx,
        "get",
        "/envs",
        _scope_headers(),
    )
    envs = response.json()
    if not envs:
        click.echo("No environment variables configured.")
        return
    click.echo(f"\n  {'Key':<30s}  Value")
    click.echo(f"  {'─' * 56}")
    for env in sorted(envs, key=lambda item: item["key"]):
        value = _display_value(env["value"], show_values)
        click.echo(f"  {env['key']:<30s}  {value}")
    click.echo()


# ---------------------------------------------------------------
# set
# ---------------------------------------------------------------


@env_group.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def set_cmd(
    ctx: click.Context,
    key: str,
    value: str,
) -> None:
    """Set an environment variable (KEY VALUE)."""
    _request(
        ctx,
        "patch",
        "/envs",
        _scope_headers(),
        json={"values": {key: value}},
    )
    click.echo(f"✓ Saved: {key}")


# ---------------------------------------------------------------
# delete
# ---------------------------------------------------------------


@env_group.command("delete")
@click.argument("key")
@click.pass_context
def delete_cmd(
    ctx: click.Context,
    key: str,
) -> None:
    """Delete an environment variable."""
    _request(
        ctx,
        "delete",
        f"/envs/{quote(key, safe='')}",
        _scope_headers(),
    )
    click.echo(f"✓ Deleted: {key}")


# ---------------------------------------------------------------
# Interactive helper (used by init_cmd)
# ---------------------------------------------------------------


def configure_env_interactive() -> None:
    """Interactively add/edit environment variables."""
    from .utils import prompt_confirm

    while True:
        key = click.prompt(
            "  Variable name",
            default="",
            show_default=False,
        ).strip()
        if not key:
            break
        envs = load_envs()
        current = envs.get(key, "")
        value = click.prompt(
            f"  Value for {key}",
            default=current or "",
            show_default=bool(current),
        )
        set_env_var(key, value)
        click.echo(f"  ✓ {key} = {value}")
        if not prompt_confirm("Add another variable?", default=False):
            break
    click.echo("Environment variable configuration complete.")
