# -*- coding: utf-8 -*-
"""Setup and initialization utilities for agent configuration.

This module handles copying markdown configuration files to
the working directory.
"""
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def copy_md_files(
    language: str,
    skip_existing: bool = False,
    workspace_dir: Path | None = None,
) -> list[str]:
    """Copy md files from agents/md_files to working directory.

    Args:
        language: Language code (e.g. 'en', 'zh')
        skip_existing: If True, skip files that already exist in working dir.
        workspace_dir: Target workspace directory. If None, uses WORKING_DIR.

    Returns:
        List of copied file names.
    """
    from ...constant import WORKING_DIR

    # Use provided workspace_dir or default to WORKING_DIR
    target_dir = workspace_dir if workspace_dir is not None else WORKING_DIR

    # Get md_files directory path with language subdirectory
    md_files_dir = Path(__file__).parent.parent / "md_files" / language

    if not md_files_dir.exists():
        logger.warning(
            "MD files directory not found: %s, falling back to 'en'",
            md_files_dir,
        )
        # Fallback to English if specified language not found
        md_files_dir = Path(__file__).parent.parent / "md_files" / "en"
        if not md_files_dir.exists():
            logger.error("Default 'en' md files not found either")
            return []

    # Ensure target directory exists
    target_dir.mkdir(parents=True, exist_ok=True)

    # Copy all .md files to target directory
    copied_files: list[str] = []
    for md_file in md_files_dir.glob("*.md"):
        target_file = target_dir / md_file.name
        if skip_existing and target_file.exists():
            logger.debug("Skipped existing md file: %s", md_file.name)
            continue
        try:
            shutil.copy2(md_file, target_file)
            logger.debug("Copied md file: %s", md_file.name)
            copied_files.append(md_file.name)
        except Exception as e:
            logger.error(
                "Failed to copy md file '%s': %s",
                md_file.name,
                e,
            )

    if copied_files:
        logger.debug(
            "Copied %d md file(s) [%s] to %s",
            len(copied_files),
            language,
            target_dir,
        )

    return copied_files


def _resolve_md_lang_dir(agents_root: Path, language: str) -> Path:
    """Return ``md_files/<language>``, falling back to ``en`` if missing."""
    md_lang_dir = agents_root / "md_files" / language
    if not md_lang_dir.exists():
        logger.warning(
            "MD lang dir not found: %s, falling back to 'en'",
            md_lang_dir,
        )
        md_lang_dir = agents_root / "md_files" / "en"
    return md_lang_dir


def _qa_fallback_language_order(language: str) -> list[str]:
    ordered: list[str] = []
    for lang_opt in (language, "en", "zh", "ru"):
        if lang_opt not in ordered:
            ordered.append(lang_opt)
    return ordered


def _copy_qa_aux_md(
    md_lang_dir: Path,
    workspace_dir: Path,
    only_if_missing: bool,
) -> list[str]:
    copied: list[str] = []
    for aux_name in ("MEMORY.md", "HEARTBEAT.md"):
        src_aux = md_lang_dir / aux_name
        dst_aux = workspace_dir / aux_name
        if not src_aux.exists():
            continue
        if only_if_missing and dst_aux.exists():
            continue
        try:
            shutil.copy2(src_aux, dst_aux)
            copied.append(aux_name)
            logger.debug("Copied builtin QA aux md: %s", aux_name)
        except OSError as e:
            logger.warning("Failed to copy %s for builtin QA: %s", aux_name, e)
    return copied


def _copy_qa_persona_md(
    qa_root: Path,
    fallback_langs: list[str],
    workspace_dir: Path,
    only_if_missing: bool,
) -> list[str]:
    copied: list[str] = []
    for persona_name in ("AGENTS.md", "PROFILE.md", "SOUL.md"):
        dst_p = workspace_dir / persona_name
        if only_if_missing and dst_p.exists():
            continue
        source_p = None
        for lang_opt in fallback_langs:
            cand = qa_root / lang_opt / persona_name
            if cand.exists():
                source_p = cand
                break
        if source_p is None:
            logger.warning(
                "Builtin QA template missing for %s (langs tried: %s)",
                persona_name,
                fallback_langs,
            )
            continue
        try:
            shutil.copy2(source_p, dst_p)
            copied.append(persona_name)
        except OSError as e:
            logger.warning(
                "Failed to copy QA persona file %s: %s",
                persona_name,
                e,
            )
    return copied


def _remove_bootstrap_from_workspace(workspace_dir: Path) -> None:
    bootstrap = workspace_dir / "BOOTSTRAP.md"
    if not bootstrap.exists():
        return
    try:
        bootstrap.unlink()
        logger.info(
            "Removed BOOTSTRAP.md from builtin QA workspace %s",
            workspace_dir,
        )
    except OSError as e:
        logger.warning("Could not remove BOOTSTRAP.md: %s", e)


def copy_builtin_qa_md_files(
    language: str,
    workspace_dir: Path | str,
    *,
    only_if_missing: bool = True,
) -> list[str]:
    """Seed or refresh builtin ``qa_agent`` markdown templates.

    Copies ``MEMORY.md`` / ``HEARTBEAT.md`` from ``md_files/<language>/`` and
    ``AGENTS.md`` / ``PROFILE.md`` / ``SOUL.md`` from
    ``md_files/qa/<language>/`` (with ``language`` → en → zh → ru fallback).
    Never copies ``BOOTSTRAP.md``; removes it from the workspace if present.

    Args:
        language: Language code (en/zh/ru).
        workspace_dir: Agent workspace root.
        only_if_missing: If True (first-time seed), skip targets that already
            exist. If False (e.g. language switch), overwrite persona and aux
            files so content matches the new language.

    Returns:
        List of copied or overwritten file names (not including removed files).
    """
    workspace_dir = Path(workspace_dir).expanduser()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    agents_root = Path(__file__).resolve().parent.parent
    md_lang_dir = _resolve_md_lang_dir(agents_root, language)
    copied_files = _copy_qa_aux_md(
        md_lang_dir,
        workspace_dir,
        only_if_missing,
    )
    qa_root = agents_root / "md_files" / "qa"
    fallback_langs = _qa_fallback_language_order(language)
    copied_files.extend(
        _copy_qa_persona_md(
            qa_root,
            fallback_langs,
            workspace_dir,
            only_if_missing,
        ),
    )
    _remove_bootstrap_from_workspace(workspace_dir)
    return copied_files


def _get_tenant_directories(
    tenant_id: str | None = None,
) -> tuple[Path, Path]:
    """Get working and secret directories for a tenant.

    Args:
        tenant_id: Tenant identifier. None uses current runtime tenant.

    Returns:
        Tuple of (working_dir, secret_dir).
    """
    from ...constant import SECRET_DIR, WORKING_DIR

    if tenant_id is not None:
        return WORKING_DIR / tenant_id, SECRET_DIR / tenant_id

    from ...config.context import get_current_tenant_id

    current_tenant = get_current_tenant_id()
    if current_tenant:
        return WORKING_DIR / current_tenant, SECRET_DIR / current_tenant
    return WORKING_DIR, SECRET_DIR


def _copy_single_file(
    src: Path,
    dst: Path,
    skip_existing: bool,
    force: bool,
) -> bool:
    """Copy a single file with skip/force logic.

    Args:
        src: Source file path.
        dst: Destination file path.
        skip_existing: Skip if destination exists.
        force: Force overwrite.

    Returns:
        True if file was copied, False otherwise.
    """
    if not src.exists():
        logger.warning("Source file not found: %s", src)
        return False

    if skip_existing and dst.exists():
        logger.debug("Skipped existing file: %s", dst.name)
        return False

    if not force and dst.exists():
        return False

    try:
        shutil.copy2(src, dst)
        logger.info("Copied %s to %s", src.name, dst)
        return True
    except Exception as e:
        logger.error("Failed to copy %s: %s", src.name, e)
        return False


def copy_init_config_files(
    tenant_id: str | None = None,
    force: bool = False,
    skip_existing: bool = False,
) -> tuple[bool, bool]:
    """Copy config.json and providers.json from md_files to tenant directories.

    This function copies the template configuration files to initialize
    model, MCP, and channel settings for a tenant.

    Args:
        tenant_id: Tenant identifier for subdirectory isolation.
                   None uses the current runtime tenant directory.
        force: If True, overwrite existing files.
        skip_existing: If True, skip files that already exist (takes precedence
                       over force when both are True).

    Returns:
        Tuple of (config_copied, providers_copied) indicating which files
        were copied.
    """
    import os

    md_files_dir = Path(__file__).parent.parent / "md_files"
    working_dir, secret_dir = _get_tenant_directories(tenant_id)

    # Create directories if needed
    working_dir.mkdir(parents=True, exist_ok=True)
    secret_dir.mkdir(parents=True, exist_ok=True)

    # Set secure permissions on secret directory
    try:
        os.chmod(secret_dir, 0o700)
    except OSError:
        pass

    # Copy config.json
    config_copied = _copy_single_file(
        md_files_dir / "config.json",
        working_dir / "config.json",
        skip_existing,
        force,
    )

    # Copy providers.json
    dst_providers = secret_dir / "providers.json"
    providers_copied = _copy_single_file(
        md_files_dir / "providers.json",
        dst_providers,
        skip_existing,
        force,
    )

    # Set secure permissions on providers.json
    if providers_copied:
        try:
            os.chmod(dst_providers, 0o600)
        except OSError:
            pass

    return config_copied, providers_copied
