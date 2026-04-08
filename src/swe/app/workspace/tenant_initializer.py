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

logger = logging.getLogger(__name__)


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

    def __init__(self, base_working_dir: Path, tenant_id: str):
        self.base_working_dir = Path(base_working_dir).expanduser().resolve()
        self.tenant_id = tenant_id
        self.tenant_dir = self.base_working_dir / tenant_id

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
        """Return True when the tenant bootstrap scaffold is present."""
        default_workspace = self.tenant_dir / "workspaces" / "default"
        required_paths = [
            self.tenant_dir / "config.json",
            default_workspace,
            default_workspace / "agent.json",
            default_workspace / "chats.json",
            default_workspace / "jobs.json",
            default_workspace / "token_usage.json",
            default_workspace / "sessions",
            default_workspace / "memory",
        ]
        required_paths.extend(
            default_workspace / filename
            for filename in self._WORKSPACE_REQUIRED_FILES
        )

        return (
            all(path.exists() for path in required_paths)
            and self._has_skill_pool_state()
        )

    def initialize_minimal(self) -> None:
        """Run minimal bootstrap sequence (idempotent).

        This is called on first tenant access and only ensures:
        - Directory structure exists
        - Default agent declaration exists

        No runtime components are started.
        """
        self.ensure_directory_structure()
        self.ensure_default_agent()

    def ensure_seeded_bootstrap(self) -> dict[str, Any]:
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
            "pool_seed": {},
            "workspace_seed": {},
            "workspace_scaffold": {},
        }
        config_existed = (self.tenant_dir / "config.json").exists()

        # Step 1: Minimal initialization
        self.initialize_minimal()
        result["minimal"] = True

        # Step 1.5: Seed tenant root config from default template on
        # first bootstrap or after external deletion.
        result["config_seed"] = self.seed_tenant_config_from_default(
            overwrite=not config_existed,
        )

        # Step 2: Seed skill pool from default (or builtin fallback)
        # Note: This raises RuntimeError on complete failure (including builtin fallback)
        result["pool_seed"] = self.seed_skill_pool_from_default()

        # Step 3: Seed default workspace skills from default tenant
        # Note: This raises RuntimeError on failure
        result["workspace_seed"] = self.seed_default_workspace_skills_from_default()

        # Step 4: Ensure the default workspace scaffold is complete.
        result["workspace_scaffold"] = self.ensure_default_workspace_scaffold()

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
        from ...agents.skills_manager import (
            get_workspace_skills_dir,
            get_workspace_skill_manifest_path,
        )

        default_workspace = self.tenant_dir / "workspaces" / "default"
        skills_dir = get_workspace_skills_dir(default_workspace)
        manifest_path = get_workspace_skill_manifest_path(default_workspace)

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
            except (json.JSONDecodeError, OSError):
                pass

            # Manifest exists (even if empty/corrupt), check for partial state
            if skills_dir.exists():
                for item in skills_dir.iterdir():
                    if item.is_dir() and (item / "SKILL.md").exists():
                        return True
        else:
            # No manifest - need seeding regardless of directory state
            pass

        return False

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
        import shutil

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

    def seed_tenant_config_from_default(
        self,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Seed tenant config.json from default tenant without copying refs."""
        from ...config.utils import load_config, save_config

        target_config_path = self.tenant_dir / "config.json"
        source_config_path = self.base_working_dir / "default" / "config.json"
        result: dict[str, Any] = {"seeded": False, "source": None}

        if not source_config_path.exists():
            return result
        if target_config_path.exists() and not overwrite:
            return result

        target_config = load_config(target_config_path)
        source_config = load_config(source_config_path)
        merged_config = source_config.model_copy(deep=True)

        # Keep tenant-local workspace refs created during minimal bootstrap.
        merged_config.agents.profiles = target_config.agents.profiles
        merged_config.agents.active_agent = (
            target_config.agents.active_agent
            or source_config.agents.active_agent
            or "default"
        )

        save_config(merged_config, target_config_path)
        result["seeded"] = True
        result["source"] = "default"
        return result

    def ensure_default_workspace_scaffold(self) -> dict[str, Any]:
        """Ensure runtime-required workspace files exist for default agent."""
        from ...agents.utils.setup_utils import copy_md_files
        from ...config.config import AgentProfileConfig, load_agent_config

        default_workspace = self.tenant_dir / "workspaces" / "default"
        default_workspace.mkdir(parents=True, exist_ok=True)

        for dirname in ("sessions", "memory", "skills"):
            (default_workspace / dirname).mkdir(parents=True, exist_ok=True)

        tenant_config_path = self.tenant_dir / "config.json"
        target_agent_config_path = default_workspace / "agent.json"
        default_template_workspace = (
            self.base_working_dir / "default" / "workspaces" / "default"
        )
        source_agent_config_path = default_template_workspace / "agent.json"
        if (
            not target_agent_config_path.exists()
            and source_agent_config_path.exists()
        ):
            agent_payload = json.loads(
                source_agent_config_path.read_text(encoding="utf-8"),
            )
            agent_payload["workspace_dir"] = str(default_workspace)
            agent_config_model = AgentProfileConfig(**agent_payload)
            target_agent_config_path.write_text(
                json.dumps(
                    agent_config_model.model_dump(exclude_none=True),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        agent_config = load_agent_config(
            "default",
            config_path=tenant_config_path,
        )

        copied_files: list[str] = []
        for filename in self._WORKSPACE_TEMPLATE_FILES:
            source_file = default_template_workspace / filename
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

        token_usage_path = default_workspace / "token_usage.json"
        if not token_usage_path.exists():
            token_usage_path.write_text("{}", encoding="utf-8")

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
                for skill_name, skill_entry in source_manifest.get("skills", {}).items():
                    if "config" in skill_entry:
                        source_skills_with_config[skill_name] = skill_entry["config"]
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

        default_working_dir = self.base_working_dir / "default"
        default_pool_dir = get_skill_pool_dir(working_dir=default_working_dir)
        default_manifest_path = get_pool_skill_manifest_path(
            working_dir=default_working_dir,
        )

        # Prepare source state (reconcile + collect config)
        source_skill_names, source_skills_with_config = self._prepare_source_pool_state(
            default_pool_dir,
            default_manifest_path,
        )

        # Try to seed from default tenant if template exists
        if source_skill_names:
            try:
                target_pool_dir = get_skill_pool_dir(working_dir=self.tenant_dir)
                copied = self._copy_skill_directories(
                    default_pool_dir,
                    target_pool_dir,
                )

                if copied:
                    # Reconcile target to build proper manifest
                    reconcile_pool_manifest(working_dir=self.tenant_dir)

                    # Preserve durable config from source manifest
                    self._merge_pool_manifest_config(source_skills_with_config)

                    result["seeded"] = True
                    result["source"] = "default"
                    result["skills"] = copied
                    return result
            except Exception as e:
                logger.warning(
                    f"Failed to seed pool from default for tenant {self.tenant_id}: {e}. "
                    "Falling back to builtin initialization.",
                )

        # Fall back to builtin initialization
        # First, check if target already has skill directories (partial copy scenario)
        # and reconcile them to preserve existing skills
        target_pool_dir = get_skill_pool_dir(working_dir=self.tenant_dir)
        if target_pool_dir.exists():
            existing_skills = [
                item.name for item in target_pool_dir.iterdir()
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
            manifest_path = get_pool_skill_manifest_path(working_dir=self.tenant_dir)
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
            get_workspace_skills_dir,
            get_workspace_skill_manifest_path,
            reconcile_workspace_manifest,
            _read_json_unlocked,
            _default_workspace_manifest,
        )

        result: dict[str, Any] = {"seeded": False, "skills": []}

        # Skip if default workspace already has skills
        if self._has_default_workspace_skills():
            return result

        default_workspace = (
            self.base_working_dir / "default" / "workspaces" / "default"
        )
        default_skills_dir = get_workspace_skills_dir(default_workspace)
        default_manifest_path = get_workspace_skill_manifest_path(
            default_workspace,
        )

        # First, reconcile source from disk to ensure we have latest state
        # This allows seeding even when source manifest is absent/stale
        source_skills_state: dict[str, Any] = {}
        if default_skills_dir.exists():
            try:
                # Read source manifest for durable state before reconcile
                if default_manifest_path.exists():
                    source_manifest = _read_json_unlocked(
                        default_manifest_path,
                        _default_workspace_manifest(),
                    )
                    # Capture durable state for each skill
                    for skill_name, skill_entry in source_manifest.get("skills", {}).items():
                        source_skills_state[skill_name] = {
                            field: skill_entry[field]
                            for field in ("enabled", "channels", "config", "source")
                            if field in skill_entry
                        }

                # Reconcile source to discover skills from disk
                reconcile_workspace_manifest(default_workspace)
            except Exception as e:
                logger.warning(
                    f"Failed to reconcile source workspace for tenant {self.tenant_id}: {e}",
                )

        # Check if source has usable skills after reconciliation
        source_skill_names: list[str] = []
        if default_skills_dir.exists():
            for item in default_skills_dir.iterdir():
                if item.is_dir() and (item / "SKILL.md").exists():
                    source_skill_names.append(item.name)

        if not source_skill_names:
            # Source has no skills, but check if target has existing skills (partial copy)
            target_workspace = self.tenant_dir / "workspaces" / "default"
            target_skills_dir = get_workspace_skills_dir(target_workspace)
            if target_skills_dir.exists():
                existing_skills = [
                    item.name for item in target_skills_dir.iterdir()
                    if item.is_dir() and (item / "SKILL.md").exists()
                ]
                if existing_skills:
                    logger.info(
                        f"Found {len(existing_skills)} existing workspace skills for "
                        f"tenant {self.tenant_id}, reconciling: {existing_skills}",
                    )
                    try:
                        reconcile_workspace_manifest(target_workspace)
                        result["seeded"] = True
                        result["skills"] = existing_skills
                        return result
                    except Exception as reconcile_error:
                        logger.warning(
                            f"Failed to reconcile existing workspace skills for "
                            f"tenant {self.tenant_id}: {reconcile_error}",
                        )
            return result

        try:
            # Copy skill directories
            target_workspace = self.tenant_dir / "workspaces" / "default"
            target_skills_dir = get_workspace_skills_dir(target_workspace)
            copied = self._copy_skill_directories(
                default_skills_dir,
                target_skills_dir,
            )

            if not copied:
                return result

            # Reconcile target to build proper manifest
            reconcile_workspace_manifest(target_workspace)

            # Preserve durable state from source manifest
            if source_skills_state:
                self._merge_workspace_manifest_state(
                    target_workspace,
                    source_skills_state,
                )

            result["seeded"] = True
            result["skills"] = copied
            return result

        except Exception as e:
            logger.error(
                f"Failed to seed workspace skills for tenant {self.tenant_id}: {e}",
            )
            raise RuntimeError(
                f"Default workspace skill seeding failed for tenant {self.tenant_id}: {e}",
            ) from e

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
