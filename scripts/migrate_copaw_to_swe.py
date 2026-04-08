#!/usr/bin/env python3
"""
Migrate ~/.copaw to ~/.swe with backup support.

This script is automatically called when SWE starts and detects
the old ~/.copaw directory exists but ~/.swe does not.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def migrate_copaw_to_swe() -> bool:
    """Migrate old ~/.copaw directory to new ~/.swe.

    Returns:
        True if migration succeeded or was not needed,
        False if migration failed (e.g., target exists).
    """
    old_dir = Path.home() / ".copaw"
    new_dir = Path.home() / ".swe"
    old_secret_dir = Path.home() / ".copaw.secret"
    new_secret_dir = Path.home() / ".swe.secret"

    # Check if migration is needed
    if not old_dir.exists() and not old_secret_dir.exists():
        logger.debug("No ~/.copaw directory found, skipping migration.")
        return True

    # Check if target already exists
    if new_dir.exists():
        logger.warning(
            "~/.swe already exists. Please manually migrate data from ~/.copaw."
        )
        return False

    # Migrate main directory
    if old_dir.exists():
        logger.info(f"Migrating {old_dir} -> {new_dir}")
        try:
            shutil.move(str(old_dir), str(new_dir))
            logger.info(f"Successfully migrated ~/.copaw to ~/.swe")
        except Exception as e:
            logger.error(f"Failed to migrate ~/.copaw: {e}")
            return False

    # Migrate secret directory
    if old_secret_dir.exists() and not new_secret_dir.exists():
        logger.info(f"Migrating {old_secret_dir} -> {new_secret_dir}")
        try:
            shutil.move(str(old_secret_dir), str(new_secret_dir))
            logger.info(f"Successfully migrated ~/.copaw.secret to ~/.swe.secret")
        except Exception as e:
            logger.error(f"Failed to migrate ~/.copaw.secret: {e}")
            # Don't fail the whole migration if only secret fails

    logger.info("Migration completed successfully!")
    return True


def check_and_migrate() -> None:
    """Check if migration is needed and perform it.

    This is called during SWE initialization.
    """
    old_dir = Path.home() / ".copaw"
    new_dir = Path.home() / ".swe"

    # Only migrate if old exists and new doesn't
    if old_dir.exists() and not new_dir.exists():
        logger.info("Detected legacy ~/.copaw directory. Migrating to ~/.swe...")
        if migrate_copaw_to_swe():
            logger.info(
                "Migration complete. Your data is now in ~/.swe. "
                "You can safely remove the backup after verification."
            )
        else:
            logger.warning(
                "Migration failed. Please manually copy data from ~/.copaw to ~/.swe"
            )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Migrate ~/.copaw to ~/.swe")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    old_dir = Path.home() / ".copaw"
    new_dir = Path.home() / ".swe"

    if args.dry_run:
        print(f"Would migrate: {old_dir} -> {new_dir}")
        old_secret = Path.home() / ".copaw.secret"
        new_secret = Path.home() / ".swe.secret"
        if old_secret.exists():
            print(f"Would migrate: {old_secret} -> {new_secret}")
    else:
        migrate_copaw_to_swe()