# -*- coding: utf-8 -*-
"""Tenant directory bootstrapper.

Creates the directory structure and seeds default agents for a single tenant.
Used by both ``swe init --tenant-id`` (CLI) and ``TenantWorkspacePool`` (runtime)
so the bootstrap logic lives in one place.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from ..migration import (
    ensure_default_agent_exists,
)
from .bootstrap_state import (
    BootstrapRecoveryFailure,
    inspect_bootstrap_readiness,
    move_to_recovery_backup,
    write_bootstrap_json,
    write_bootstrap_ready_marker,
)

logger = logging.getLogger(__name__)


def _inherit_zhaohu_from_template(
    base_channels: dict[str, Any],
    template_payload: dict[str, Any],
) -> dict[str, Any]:
    """新租户 zhaohu 配置继承自 source 模板（default 用户）agent.json。

    以 base_channels（config.json + env 默认）为基准，用模板 agent.json 的
    zhaohu 非空字段覆盖同名字段；空值不覆盖，保留默认兜底。
    """
    channels = dict(base_channels)
    template_channels = template_payload.get("channels") or {}
    tpl_zhaohu = template_channels.get("zhaohu")
    if not isinstance(tpl_zhaohu, dict) or not tpl_zhaohu:
        return channels
    merged = dict(channels.get("zhaohu") or {})
    for key, value in tpl_zhaohu.items():
        if (
            value is None
            or value == ""
            or (isinstance(value, (list, dict)) and not value)
        ):
            continue
        merged[key] = value
    channels["zhaohu"] = merged
    return channels


class TenantInitializer:
    """Bootstrap a tenant directory with required structure and agents."""

    _WORKSPACE_TEMPLATE_FILES = (
        "AGENTS.md",
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
        "MEMORY.md",
        "PROFILE.md",
        "SOUL.md",
    )
    _WORKSPACE_REQUIRED_FILES = tuple(
        filename
        for filename in _WORKSPACE_TEMPLATE_FILES
        if filename != "BOOTSTRAP.md"
    )

    def __init__(
        self,
        base_working_dir: Path,
        tenant_id: str,
        source_id: str | None = None,
        scope_id: str | None = None,
    ):
        """Initialize tenant bootstrapper.

        Args:
            base_working_dir: Base working directory (~/.swe).
            tenant_id: The tenant identifier.
            source_id: Optional source identifier from X-Source-Id header.
                Used to select the appropriate default_{source} template.
                Runtime-scoped working directories use the encoded scope_id
                when source_id is present.
            scope_id: Optional explicit runtime scope. When provided, it takes
                precedence over tenant/source recomputation.
        """
        from ...config.context import resolve_storage_tenant_id

        self.base_working_dir = Path(base_working_dir).expanduser().resolve()
        self.tenant_id = tenant_id
        self.source_id = source_id or None
        self.scope_id = scope_id or None
        self.template_name = self._resolve_template_name()
        self.effective_tenant_id = (
            resolve_storage_tenant_id(
                tenant_id,
                self.source_id,
                scope_id=self.scope_id,
            )
            or tenant_id
        )
        self.tenant_dir = self.base_working_dir / self.effective_tenant_id

    def _resolve_template_name(self) -> str:
        """Return the explicitly provisioned source template name."""
        if not self.source_id:
            return "default"
        return f"default_{self.source_id}"

    def ensure_directory_structure(self) -> None:
        """Create the tenant directory skeleton (minimal bootstrap)."""
        for path in (
            self.tenant_dir,
            self.tenant_dir / "workspaces",
            self.tenant_dir / "media",
            self.tenant_dir / "secrets",
        ):
            path.mkdir(parents=True, exist_ok=True)

    def ensure_default_agent(self) -> None:
        """Ensure the default agent workspace exists (minimal bootstrap).

        This only creates the agent declaration and directory structure,
        not the runtime.
        """
        ensure_default_agent_exists(working_dir=self.tenant_dir)

    def has_seeded_bootstrap(self) -> bool:
        """Return True only when real bootstrap artifacts are strictly ready."""
        return inspect_bootstrap_readiness(self.tenant_dir).ready

    def recover_seeded_bootstrap(
        self,
        *,
        enable_bootstrap_chat: bool = True,
    ) -> dict[str, list[str]]:
        """Repair missing or invalid bootstrap-owned artifacts.

        Only JSON paths rejected by strict readiness are moved aside. Backups
        from this recovery are removed immediately after final readiness.
        """
        readiness = inspect_bootstrap_readiness(self.tenant_dir)
        if readiness.ready:
            return {"recovered_paths": []}

        backups: list[Path] = []
        recovered_paths: list[str] = []
        try:
            for invalid_path in readiness.invalid_json_paths:
                if invalid_path.is_file():
                    backups.append(move_to_recovery_backup(invalid_path))
                    recovered_paths.append(str(invalid_path))
            self._reconcile_stale_workspace_skills()
            self.ensure_seeded_bootstrap(
                enable_bootstrap_chat=enable_bootstrap_chat,
            )
            final_readiness = inspect_bootstrap_readiness(self.tenant_dir)
            if not final_readiness.ready:
                raise BootstrapRecoveryFailure(
                    f"tenant bootstrap remains {final_readiness.reason}",
                )
            write_bootstrap_ready_marker(self.tenant_dir)
        except Exception as exc:
            if isinstance(exc, BootstrapRecoveryFailure):
                raise
            raise BootstrapRecoveryFailure(
                "tenant bootstrap recovery failed",
            ) from exc
        for backup_path in backups:
            backup_path.unlink(missing_ok=True)
        return {"recovered_paths": recovered_paths}

    def _reconcile_stale_workspace_skills(self) -> None:
        """Remove stale registered workspace skills before bootstrap seeding."""
        from ...agents.skills_manager import reconcile_workspace_manifest

        workspace_dir = self.tenant_dir / "workspaces" / "default"
        manifest_path = workspace_dir / "skill.json"
        if manifest_path.is_file():
            reconcile_workspace_manifest(workspace_dir)

    def initialize_minimal(self) -> None:
        """Run minimal bootstrap sequence (idempotent).

        This is called on first tenant access and only ensures:
        - Directory structure exists
        - Default agent declaration exists

        No runtime components are started.
        """
        self.ensure_directory_structure()
        self.ensure_default_agent()

    def ensure_seeded_bootstrap(
        self,
        *,
        enable_bootstrap_chat: bool = True,
    ) -> dict[str, Any]:
        """Run seeded bootstrap sequence (idempotent, runtime-safe).

        This is called on first tenant access and ensures:
        - Directory structure exists
        - Default agent declaration exists
        - Skill pool is seeded from default tenant (or builtin fallback)
        - Default workspace skills are seeded from default tenant

        Does NOT create the QA agent or start workspace runtime.

        Raises:
            RuntimeError: If skill pool seeding fails (including builtin fallback).

        Returns:
            Dict with bootstrap results:
            - "minimal": True if minimal init completed
            - "pool_seed": result from seed_skill_pool_from_default()
            - "workspace_seed": result from seed_default_workspace_skills_from_default()
        """
        result: dict[str, Any] = {
            "minimal": False,
            "config_seed": {},
            "providers_seed": {},
            "pool_seed": {},
            "workspace_seed": {},
            "workspace_scaffold": {},
        }
        is_default_tenant = self.tenant_id == "default"
        config_existed = (self.tenant_dir / "config.json").exists()

        # Step 1: Minimal initialization
        self.initialize_minimal()
        result["minimal"] = True

        # Step 1.5: Seed tenant root config from template when missing。
        # 历史逻辑假设非 default 租户的 config 会在其他链路提前复制，
        # 但 source 模板场景下首次初始化需要在这里统一补齐。
        if is_default_tenant or not config_existed:
            result["config_seed"] = self.seed_tenant_config_from_default(
                overwrite=not config_existed,
            )

        # Step 1.6: Seed providers directory from default tenant
        result["providers_seed"] = self.seed_providers_from_default(
            overwrite=False,
        )

        # Step 2: Seed skill pool from default (or builtin fallback)
        # Note: This raises RuntimeError on complete failure (including builtin fallback)
        result["pool_seed"] = self.seed_skill_pool_from_default()

        # Step 3: Seed default workspace skills from default tenant
        # Note: This raises RuntimeError on failure
        result["workspace_seed"] = (
            self.seed_default_workspace_skills_from_default()
        )

        # Step 4: Ensure the default workspace scaffold is complete.
        result["workspace_scaffold"] = self.ensure_default_workspace_scaffold(
            enable_bootstrap_chat=enable_bootstrap_chat,
        )

        return result

    def initialize(self) -> dict[str, Any]:
        """Run full tenant initialization (backward compatibility alias).

        This is an alias for initialize_full() for backward compatibility
        with existing code and tests.

        Returns:
            Dict with initialization results (see initialize_full()).
        """
        return self.initialize_full()

    def _has_skill_pool_state(self) -> bool:
        """Check if tenant already has skill pool state.

        Uses manifest as primary source of truth. Only falls back to
        directory checking if manifest exists but is empty/corrupt.

        Returns:
            True if skill pool manifest exists with skills, False otherwise.
        """
        from ...agents.skills_manager import (
            get_skill_pool_dir,
            get_pool_skill_manifest_path,
        )

        pool_dir = get_skill_pool_dir(working_dir=self.tenant_dir)
        manifest_path = get_pool_skill_manifest_path(
            working_dir=self.tenant_dir,
        )

        # Primary check: manifest exists and has skills
        # If manifest doesn't exist, we need seeding (even if directories exist)
        if manifest_path.exists():
            try:
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8"),
                )
                if manifest.get("skills"):
                    return True
                # Manifest exists but is empty - check if skills were partially copied
                # This handles the case where manifest was deleted but skills remain
            except (json.JSONDecodeError, OSError):
                pass

            # Manifest exists (even if empty/corrupt), check for partial state
            if pool_dir.exists():
                for item in pool_dir.iterdir():
                    if item.is_dir() and (item / "SKILL.md").exists():
                        return True
        else:
            # No manifest - need seeding regardless of directory state
            # This prevents partial copy from being considered "initialized"
            pass

        return False

    def _has_default_workspace_skills(self) -> bool:
        """Check if default workspace already has skill state.

        Uses manifest as primary source of truth. Only falls back to
        directory checking if manifest exists but is empty/corrupt.

        Returns:
            True if default workspace has skill manifest with skills, False otherwise.
        """
        from ...agents.skills_manager import get_workspace_skill_manifest_path

        default_workspace = self.tenant_dir / "workspaces" / "default"
        manifest_path = get_workspace_skill_manifest_path(default_workspace)

        if not manifest_path.exists():
            return False
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
        )
        return (
            bool(manifest.get("skills"))
            and manifest.get(
                "layout_version",
            )
            == 2
        )

    def _copy_skill_directories(
        self,
        source_dir: Path,
        target_dir: Path,
    ) -> list[str]:
        """Copy skill directories from source to target.

        Args:
            source_dir: Source directory containing skill subdirectories.
            target_dir: Target directory to copy skills into.

        Returns:
            List of copied skill names.
        """

        copied: list[str] = []

        if not source_dir.exists():
            return copied

        target_dir.mkdir(parents=True, exist_ok=True)

        for item in source_dir.iterdir():
            if item.is_dir() and (item / "SKILL.md").exists():
                target_skill_dir = target_dir / item.name
                if target_skill_dir.exists():
                    shutil.rmtree(target_skill_dir)
                shutil.copytree(item, target_skill_dir)
                copied.append(item.name)

        return copied

    def _list_skill_directories(self, skills_dir: Path) -> list[str]:
        """List skill directory names in a given directory.

        Args:
            skills_dir: Directory containing skill subdirectories.

        Returns:
            List of skill directory names that contain SKILL.md.
        """
        if not skills_dir.exists():
            return []
        return [
            item.name
            for item in skills_dir.iterdir()
            if item.is_dir() and (item / "SKILL.md").exists()
        ]

    def seed_tenant_config_from_default(
        self,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Seed tenant config.json from default tenant.

        Copies the config file directly and updates workspace paths
        to point to the new tenant's directories.
        """

        target_config_path = self.tenant_dir / "config.json"
        source_config_path = (
            self.base_working_dir / self.template_name / "config.json"
        )
        result: dict[str, Any] = {"seeded": False, "source": None}

        if not source_config_path.exists():
            return result
        if target_config_path.exists() and not overwrite:
            return result

        try:
            # Read source config as raw JSON to preserve exact content
            source_content = source_config_path.read_text(encoding="utf-8")
            source_config = json.loads(source_content)

            # Update workspace_dir paths to point to new tenant
            template_workspace_prefix = str(
                self.base_working_dir / self.template_name / "workspaces",
            )
            tenant_workspace_prefix = str(self.tenant_dir / "workspaces")

            # Update profiles workspace_dir
            if (
                "agents" in source_config
                and "profiles" in source_config["agents"]
            ):
                for profile in source_config["agents"]["profiles"].values():
                    if "workspace_dir" in profile:
                        old_path = profile["workspace_dir"]
                        # Replace template tenant path with new tenant path
                        if old_path.startswith(template_workspace_prefix):
                            profile["workspace_dir"] = old_path.replace(
                                template_workspace_prefix,
                                tenant_workspace_prefix,
                            )

            # Write the modified config
            write_bootstrap_json(target_config_path, source_config)
            result["seeded"] = True
            result["source"] = self.template_name
        except Exception as e:
            logger.warning(
                f"Failed to seed config from {self.template_name} for tenant "
                f"{self.tenant_id}: {e}",
            )

        return result

    def seed_providers_from_default(
        self,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Seed tenant providers directory from default tenant.

        Copies the entire providers directory structure from the default tenant,
        including builtin/, custom/, and active_model.json.

        If the source-specific template providers directory doesn't exist,
        automatically creates it from the default providers directory.

        Args:
            overwrite: If True, overwrite existing providers directory.

        Returns:
            Dict with result status:
            - "seeded": True if seeded, False otherwise
            - "source": template name or None
        """
        from ...constant import SECRET_DIR

        target_providers_dir = (
            SECRET_DIR / self.effective_tenant_id / "providers"
        )
        source_providers_dir = SECRET_DIR / self.template_name / "providers"
        result: dict[str, Any] = {"seeded": False, "source": None}

        # When template and target are identical, no copying is needed.
        if self.template_name == self.effective_tenant_id:
            if source_providers_dir.exists():
                result["seeded"] = True
                result["source"] = self.template_name
            return result

        if not source_providers_dir.exists():
            return result
        if not any(source_providers_dir.iterdir()):
            return result

        # Check if target already exists
        if target_providers_dir.exists() and not overwrite:
            return result

        try:
            # Remove existing target if overwrite is True
            if target_providers_dir.exists():
                shutil.rmtree(target_providers_dir)

            # Copy entire providers directory
            shutil.copytree(source_providers_dir, target_providers_dir)
            result["seeded"] = True
            result["source"] = self.template_name
            logger.info(
                f"Seeded providers directory from {self.template_name} "
                f"for tenant {self.effective_tenant_id}",
            )
        except Exception as e:
            logger.warning(
                f"Failed to seed providers from {self.template_name} "
                f"for tenant {self.effective_tenant_id}: {e}",
            )

        return result

    def ensure_default_workspace_scaffold(
        self,
        *,
        enable_bootstrap_chat: bool = True,
    ) -> dict[str, Any]:
        """Ensure runtime-required workspace files exist for default agent."""
        from ...agents.utils.setup_utils import copy_md_files
        from ...config.config import (
            AgentProfileConfig,
            load_agent_config,
            save_agent_config,
        )
        from ...config.utils import load_config

        default_workspace = self.tenant_dir / "workspaces" / "default"
        default_workspace.mkdir(parents=True, exist_ok=True)

        for dirname in ("sessions", "memory", "skills"):
            (default_workspace / dirname).mkdir(parents=True, exist_ok=True)

        tenant_config_path = self.tenant_dir / "config.json"
        tenant_config = load_config(tenant_config_path)
        target_agent_config_path = default_workspace / "agent.json"
        template_workspace = (
            self.base_working_dir
            / self.template_name
            / "workspaces"
            / "default"
        )
        source_agent_config_path = template_workspace / "agent.json"
        if (
            not target_agent_config_path.exists()
            and source_agent_config_path.exists()
        ):
            agent_payload = json.loads(
                source_agent_config_path.read_text(encoding="utf-8"),
            )
            agent_payload["workspace_dir"] = str(default_workspace)
            channels = tenant_config.channels.model_dump(exclude_none=True)
            # 继承 source 下 default 用户（模板）agent.json 的 zhaohu 配置
            agent_payload["channels"] = _inherit_zhaohu_from_template(
                channels,
                agent_payload,
            )
            agent_config_model = AgentProfileConfig(**agent_payload)
            write_bootstrap_json(
                target_agent_config_path,
                agent_config_model.model_dump(exclude_none=True),
            )
        elif not target_agent_config_path.exists():
            save_agent_config(
                "default",
                AgentProfileConfig(
                    id="default",
                    name="Default Agent",
                    description="Default SWE agent",
                    workspace_dir=str(default_workspace),
                    channels=tenant_config.channels,
                    mcp=tenant_config.mcp,
                    heartbeat=(
                        tenant_config.agents.defaults.heartbeat
                        if tenant_config.agents.defaults
                        else None
                    ),
                    running=tenant_config.agents.running,
                    llm_routing=tenant_config.agents.llm_routing,
                    language=tenant_config.agents.language or "zh",
                    system_prompt_files=(
                        tenant_config.agents.system_prompt_files
                    ),
                    tools=tenant_config.tools,
                    security=tenant_config.security,
                ),
                config_path=tenant_config_path,
            )

        agent_config = load_agent_config(
            "default",
            config_path=tenant_config_path,
        )

        copied_files: list[str] = []
        for filename in self._WORKSPACE_TEMPLATE_FILES:
            source_file = template_workspace / filename
            target_file = default_workspace / filename
            if target_file.exists():
                continue
            if source_file.exists():
                shutil.copy2(source_file, target_file)
                copied_files.append(filename)

        copied_files.extend(
            copy_md_files(
                agent_config.language or "zh",
                skip_existing=True,
                workspace_dir=default_workspace,
            ),
        )
        if not enable_bootstrap_chat:
            bootstrap_path = default_workspace / "BOOTSTRAP.md"
            if bootstrap_path.exists():
                bootstrap_path.unlink()
            copied_files = [
                filename
                for filename in copied_files
                if filename != "BOOTSTRAP.md"
            ]

        token_usage_path = default_workspace / "token_usage.json"
        if not token_usage_path.exists():
            write_bootstrap_json(token_usage_path, {})

        return {
            "agent_json": (default_workspace / "agent.json").exists(),
            "copied_files": sorted(set(copied_files)),
            "token_usage": token_usage_path.exists(),
        }

    def _prepare_source_pool_state(
        self,
        default_pool_dir: Path,
        default_manifest_path: Path,
    ) -> tuple[list[str], dict[str, Any]]:
        """Prepare source pool state for seeding.

        Reconciles source from disk and extracts config preservation data.

        Returns:
            Tuple of (source_skill_names, source_skills_with_config).
        """
        from ...agents.skills_manager import (
            reconcile_pool_manifest,
            _read_json_unlocked,
            _default_pool_manifest,
        )

        source_skills_with_config: dict[str, Any] = {}

        # Read source manifest for durable state (config) before reconcile
        if default_manifest_path.exists():
            try:
                source_manifest = _read_json_unlocked(
                    default_manifest_path,
                    _default_pool_manifest(),
                )
                # Capture config for each skill to preserve after copy
                for skill_name, skill_entry in source_manifest.get(
                    "skills",
                    {},
                ).items():
                    if "config" in skill_entry:
                        source_skills_with_config[skill_name] = skill_entry[
                            "config"
                        ]
            except Exception as e:
                logger.warning(f"Failed to read source manifest: {e}")

        # Reconcile source to discover skills from disk
        try:
            reconcile_pool_manifest(working_dir=default_pool_dir.parent.parent)
        except Exception as e:
            logger.warning(f"Failed to reconcile source pool: {e}")

        # Collect source skill names from disk
        source_skill_names: list[str] = []
        if default_pool_dir.exists():
            for item in default_pool_dir.iterdir():
                if item.is_dir() and (item / "SKILL.md").exists():
                    source_skill_names.append(item.name)

        return source_skill_names, source_skills_with_config

    def seed_skill_pool_from_default(self) -> dict[str, Any]:
        """Seed skill pool from default tenant (idempotent).

        Copies skill_pool content from default tenant when target tenant
        has no skill pool state yet. Falls back to builtin initialization
        when no template exists.

        Uses filesystem skill directories as source of truth, reconciling
        source from disk before checking template availability.

        Returns:
            Dict with result status:
            - "seeded": True if seeded from default, False otherwise
            - "source": "default", "builtin", or None
            - "skills": list of skill names copied (if any)
        """
        from ...agents.skills_manager import (
            get_skill_pool_dir,
            get_pool_skill_manifest_path,
            import_builtin_skills,
            reconcile_pool_manifest,
            _read_json_unlocked,
            _default_pool_manifest,
        )

        result: dict[str, Any] = {
            "seeded": False,
            "source": None,
            "skills": [],
        }

        # Skip if tenant already has skill pool state
        if self._has_skill_pool_state():
            return result

        template_working_dir = self.base_working_dir / self.template_name
        template_pool_dir = get_skill_pool_dir(
            working_dir=template_working_dir,
        )
        template_manifest_path = get_pool_skill_manifest_path(
            working_dir=template_working_dir,
        )

        # Prepare source state (reconcile + collect config)
        (
            source_skill_names,
            source_skills_with_config,
        ) = self._prepare_source_pool_state(
            template_pool_dir,
            template_manifest_path,
        )

        # Try to seed from template if template exists
        if source_skill_names:
            try:
                target_pool_dir = get_skill_pool_dir(
                    working_dir=self.tenant_dir,
                )
                copied = self._copy_skill_directories(
                    template_pool_dir,
                    target_pool_dir,
                )

                if copied:
                    # Reconcile target to build proper manifest
                    reconcile_pool_manifest(working_dir=self.tenant_dir)

                    # Preserve durable config from source manifest
                    self._merge_pool_manifest_config(source_skills_with_config)

                    result["seeded"] = True
                    result["source"] = self.template_name
                    result["skills"] = copied
                    return result
            except Exception as e:
                logger.warning(
                    f"Failed to seed pool from {self.template_name} for tenant "
                    f"{self.tenant_id}: {e}. "
                    "Falling back to builtin initialization.",
                )

        # Fall back to builtin initialization
        # First, check if target already has skill directories (partial copy scenario)
        # and reconcile them to preserve existing skills
        target_pool_dir = get_skill_pool_dir(working_dir=self.tenant_dir)
        if target_pool_dir.exists():
            existing_skills = [
                item.name
                for item in target_pool_dir.iterdir()
                if item.is_dir() and (item / "SKILL.md").exists()
            ]
            if existing_skills:
                logger.info(
                    f"Found {len(existing_skills)} existing skills in pool for tenant "
                    f"{self.tenant_id}, reconciling before builtin fallback: "
                    f"{existing_skills}",
                )
                try:
                    reconcile_pool_manifest(working_dir=self.tenant_dir)
                    # If reconcile succeeds, consider seeding successful with existing skills
                    result["seeded"] = True
                    result["source"] = "existing"
                    result["skills"] = existing_skills
                    return result
                except Exception as reconcile_error:
                    logger.warning(
                        f"Failed to reconcile existing pool skills for tenant "
                        f"{self.tenant_id}: {reconcile_error}. "
                        f"Continuing with builtin fallback.",
                    )

        try:
            import_builtin_skills(working_dir=self.tenant_dir)
            result["seeded"] = True
            result["source"] = "builtin"
            result["skills"] = list(
                _read_json_unlocked(
                    get_pool_skill_manifest_path(working_dir=self.tenant_dir),
                    _default_pool_manifest(),
                )
                .get("skills", {})
                .keys(),
            )
        except Exception as e:
            logger.error(
                f"Failed to initialize builtin skills for tenant {self.tenant_id}: {e}",
            )
            raise RuntimeError(
                f"Skill pool initialization failed for tenant {self.tenant_id}: "
                f"both default tenant seeding and builtin fallback failed: {e}",
            ) from e

        return result

    def _merge_pool_manifest_config(
        self,
        source_skills_config: dict[str, Any],
    ) -> None:
        """Merge durable config from source manifest into target pool manifest.

        Args:
            source_skills_config: Dict mapping skill name to config value.
        """
        from ...agents.skills_manager import (
            get_pool_skill_manifest_path,
            _read_json_unlocked,
            _write_json_atomic,
            _default_pool_manifest,
        )

        if not source_skills_config:
            return

        try:
            manifest_path = get_pool_skill_manifest_path(
                working_dir=self.tenant_dir,
            )
            manifest = _read_json_unlocked(
                manifest_path,
                _default_pool_manifest(),
            )
            skills = manifest.get("skills", {})

            # Merge config for matching skills
            for skill_name, config in source_skills_config.items():
                if skill_name in skills:
                    skills[skill_name]["config"] = config

            _write_json_atomic(manifest_path, manifest)
        except Exception as e:
            logger.warning(
                f"Failed to merge pool config for tenant {self.tenant_id}: {e}",
            )

    def _reconcile_existing_workspace_skills(
        self,
        target_workspace: Path,
        existing_skills: list[str],
    ) -> dict[str, Any]:
        """Reconcile existing workspace skills and return result.

        Args:
            target_workspace: Target workspace directory.
            existing_skills: List of existing skill names.

        Returns:
            Result dict with seeded=True and skills list.
        """
        from ...agents.skills_manager import reconcile_workspace_manifest

        logger.info(
            f"Found {len(existing_skills)} existing workspace skills for "
            f"tenant {self.tenant_id}, reconciling: {existing_skills}",
        )
        try:
            reconcile_workspace_manifest(target_workspace)
            return {"seeded": True, "skills": existing_skills}
        except Exception as e:
            logger.warning(
                f"Failed to reconcile existing workspace skills for "
                f"tenant {self.tenant_id}: {e}",
            )
            return {"seeded": False, "skills": []}

    def _prepare_source_workspace_state(
        self,
        default_workspace: Path,
    ) -> dict[str, Any]:
        """Prepare source workspace state for seeding.

        Reconciles source workspace and captures durable skill state.

        Args:
            default_workspace: Default tenant's workspace directory.

        Returns:
            Dict with skill states (enabled, channels, config, source).
        """
        from ...agents.skills_manager import reconcile_workspace_manifest

        try:
            source_manifest = reconcile_workspace_manifest(default_workspace)
            return {
                skill_name: dict(skill_entry)
                for skill_name, skill_entry in source_manifest.get(
                    "skills",
                    {},
                ).items()
            }
        except Exception as e:
            logger.warning(
                f"Failed to reconcile source workspace for tenant {self.tenant_id}: {e}",
            )
            return {}

    def _seed_prepared_workspace_skills(
        self,
        template_workspace: Path,
        source_skills_state: dict[str, Any],
        existing_target_manifest: dict[str, Any],
        original_target_manifest: bytes | None,
    ) -> dict[str, Any]:
        """Copy prepared skills under the workspace publication lock."""
        from ...agents.skill_runtime_snapshot import (
            workspace_skill_coordinator,
        )

        target_workspace = self.tenant_dir / "workspaces" / "default"
        with workspace_skill_coordinator(target_workspace):
            return self._seed_prepared_workspace_skills_locked(
                template_workspace,
                source_skills_state,
                existing_target_manifest,
                original_target_manifest,
            )

    def _seed_prepared_workspace_skills_locked(
        self,
        template_workspace: Path,
        source_skills_state: dict[str, Any],
        existing_target_manifest: dict[str, Any],
        original_target_manifest: bytes | None,
    ) -> dict[str, Any]:
        """Copy prepared registered packages and publish target state."""
        from ...agents.skills_manager import (
            _default_workspace_manifest,
            _write_json_atomic,
            get_workspace_skill_manifest_path,
            reconcile_workspace_manifest,
            resolve_workspace_managed_skill_dir,
        )

        result: dict[str, Any] = {"seeded": False, "skills": []}
        target_workspace = self.tenant_dir / "workspaces" / "default"
        target_manifest_path = get_workspace_skill_manifest_path(
            target_workspace,
        )
        attempted_target_dirs: list[Path] = []
        try:
            copied: list[str] = []
            for skill_name, skill_entry in sorted(
                source_skills_state.items(),
            ):
                enabled = bool(skill_entry.get("enabled", False))
                source_dir = resolve_workspace_managed_skill_dir(
                    template_workspace,
                    skill_name,
                    enabled=enabled,
                )
                if not source_dir.exists():
                    continue
                target_dir = resolve_workspace_managed_skill_dir(
                    target_workspace,
                    skill_name,
                    enabled=enabled,
                )
                opposite_target_dir = resolve_workspace_managed_skill_dir(
                    target_workspace,
                    skill_name,
                    enabled=not enabled,
                )
                if skill_name not in existing_target_manifest.get(
                    "skills",
                    {},
                ) and (target_dir.exists() or opposite_target_dir.exists()):
                    continue
                attempted_target_dirs.append(target_dir)
                target_dir.parent.mkdir(parents=True, exist_ok=True)
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                shutil.copytree(source_dir, target_dir)
                copied.append(skill_name)

            if not copied:
                return result

            target_manifest = _default_workspace_manifest()
            target_manifest["version"] = 1
            target_manifest["skills"] = {
                skill_name: dict(source_skills_state[skill_name])
                for skill_name in copied
            }
            _write_json_atomic(target_manifest_path, target_manifest)
            reconciled = reconcile_workspace_manifest(target_workspace)
            reconciled["version"] = 1
            for skill_name in copied:
                source_entry = source_skills_state[skill_name]
                target_entry = reconciled["skills"][skill_name]
                for field in (
                    "enabled",
                    "channels",
                    "config",
                    "source",
                    "created_at",
                    "updated_at",
                ):
                    if field in source_entry:
                        target_entry[field] = source_entry[field]
            _write_json_atomic(target_manifest_path, reconciled)

            result["seeded"] = True
            result["skills"] = sorted(copied)
            return result
        except Exception as e:
            for attempted_dir in attempted_target_dirs:
                if attempted_dir.exists():
                    shutil.rmtree(attempted_dir, ignore_errors=True)
            if original_target_manifest is None:
                target_manifest_path.unlink(missing_ok=True)
            else:
                target_manifest_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )
                target_manifest_path.write_bytes(original_target_manifest)
            logger.error(
                f"Failed to seed workspace skills for tenant {self.tenant_id}: {e}",
            )
            raise RuntimeError(
                f"Default workspace skill seeding failed for tenant {self.tenant_id}: {e}",
            ) from e

    def seed_default_workspace_skills_from_default(self) -> dict[str, Any]:
        """Seed default workspace skills from default tenant (idempotent).

        Copies skills from default tenant's default workspace when target
        workspace has no skill state yet. Preserves enabled, channels,
        config, and source fields from source manifest.

        Uses filesystem skill directories as source of truth, reconciling
        source from disk before checking template availability.

        Returns:
            Dict with result status:
            - "seeded": True if seeded, False otherwise
            - "skills": list of skill names copied (if any)
        """
        from ...agents.skills_manager import (
            _default_workspace_manifest,
            get_workspace_skill_manifest_path,
        )

        result: dict[str, Any] = {"seeded": False, "skills": []}

        target_workspace = self.tenant_dir / "workspaces" / "default"
        target_manifest_path = get_workspace_skill_manifest_path(
            target_workspace,
        )
        target_manifest_existed = target_manifest_path.exists()
        original_target_manifest = (
            target_manifest_path.read_bytes()
            if target_manifest_existed
            else None
        )

        # Skip if default workspace already has skills
        if self._has_default_workspace_skills():
            return result

        existing_target_manifest = (
            json.loads(original_target_manifest.decode("utf-8"))
            if original_target_manifest is not None
            else _default_workspace_manifest()
        )

        template_workspace = (
            self.base_working_dir
            / self.template_name
            / "workspaces"
            / "default"
        )
        source_skills_state = self._prepare_source_workspace_state(
            template_workspace,
        )
        if not source_skills_state:
            return result

        return self._seed_prepared_workspace_skills(
            template_workspace,
            source_skills_state,
            existing_target_manifest,
            original_target_manifest,
        )

    def _merge_workspace_manifest_state(
        self,
        target_workspace: Path,
        source_skills: dict[str, Any],
    ) -> None:
        """Merge user-state fields from source manifest into target.

        Preserves enabled, channels, config, and source fields for skills
        that exist in both manifests.

        Args:
            target_workspace: Target workspace directory.
            source_skills: Source manifest skills dict with user-state.
        """
        from ...agents.skills_manager import (
            get_workspace_skill_manifest_path,
            _read_json_unlocked,
            _write_json_atomic,
            _default_workspace_manifest,
            _timestamp,
        )

        manifest_path = get_workspace_skill_manifest_path(target_workspace)
        target_manifest = _read_json_unlocked(
            manifest_path,
            _default_workspace_manifest(),
        )
        target_skills = target_manifest.get("skills", {})

        # Merge user-state fields for matching skills
        for skill_name in target_skills:
            if skill_name in source_skills:
                source_entry = source_skills[skill_name]
                target_entry = target_skills[skill_name]

                # Preserve user-state fields
                for field in ("enabled", "channels", "config", "source"):
                    if field in source_entry:
                        target_entry[field] = source_entry[field]

                target_entry["updated_at"] = _timestamp()

        target_manifest["skills"] = target_skills
        _write_json_atomic(manifest_path, target_manifest)

    def ensure_qa_agent(self) -> None:
        """Ensure the builtin QA agent workspace exists.

        This creates the QA agent declaration and seeds its skills
        from the tenant's skill pool.
        """
        from ..migration import ensure_qa_agent_exists

        ensure_qa_agent_exists(working_dir=self.tenant_dir)

    def ensure_skill_pool(self) -> None:
        """Ensure the tenant skill pool is initialized.

        This initializes the skill pool using the builtin skills.
        For full initialization with seeding from default tenant,
        use initialize_full() instead.
        """
        from ...agents.skills_manager import ensure_skill_pool_initialized

        ensure_skill_pool_initialized(working_dir=self.tenant_dir)

    def initialize_full(self) -> dict[str, Any]:
        """Run full tenant initialization with skill seeding.

        This reuses ensure_seeded_bootstrap() for the runtime-safe seeding,
        plus creates the QA agent workspace (full-init only).

        Returns:
            Dict with initialization results:
            - "minimal": True if minimal initialization completed
            - "pool_seed": result from seed_skill_pool_from_default()
            - "workspace_seed": result from seed_default_workspace_skills_from_default()
            - "qa_agent": True if QA agent created
        """
        # Reuse the runtime-safe seeded bootstrap (no QA agent)
        result = self.ensure_seeded_bootstrap()

        # Full initialization also creates the QA agent
        try:
            self.ensure_qa_agent()
            result["qa_agent"] = True
        except Exception as e:
            logger.warning(
                f"Failed to create QA agent for tenant {self.tenant_id}: {e}",
            )
            result["qa_agent"] = False

        return result
