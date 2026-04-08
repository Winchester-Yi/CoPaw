# -*- coding: utf-8 -*-
"""Agent Markdown manager for reading and writing markdown files in working
and memory directories."""
from datetime import datetime
from pathlib import Path

from ..utils.file_handling import read_text_file_with_encoding_fallback
from ..skills_manager import suggest_conflict_name
from ...utils.fs_text import sanitize_fs_text


class AgentMdManager:
    """Manager for reading and writing markdown files in working and memory
    directories."""

    def __init__(self, working_dir: str | Path):
        """Initialize directories for working and memory markdown files."""
        self.working_dir: Path = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir: Path = self.working_dir / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def _sanitize_md_filenames(self, base_dir: Path) -> None:
        existing_names = {path.name for path in base_dir.glob("*.md")}
        for file_path in sorted(base_dir.glob("*.md")):
            sanitized = sanitize_fs_text(file_path.name)
            if not sanitized.changed or sanitized.value == file_path.name:
                continue

            target_name = sanitized.value or file_path.name
            existing_names.discard(file_path.name)
            if target_name in existing_names:
                target_name = f"{suggest_conflict_name(Path(target_name).stem, existing_names)}.md"
            file_path.rename(base_dir / target_name)
            existing_names.add(target_name)

    def _resolve_md_path(
        self,
        base_dir: Path,
        md_name: str,
    ) -> Path:
        if not md_name.endswith(".md"):
            md_name += ".md"

        self._sanitize_md_filenames(base_dir)

        exact_path = base_dir / md_name
        if exact_path.exists():
            return exact_path

        for file_path in base_dir.glob("*.md"):
            sanitized = sanitize_fs_text(file_path.name)
            if sanitized.value == md_name:
                return file_path

        sanitized_name = sanitize_fs_text(md_name).value or md_name
        return base_dir / sanitized_name

    def list_working_mds(self) -> list[dict]:
        """List all markdown files with metadata in the working dir.

        Returns:
            list[dict]: A list of dictionaries, each containing:
                - filename: name of the file (with .md extension)
                - size: file size in bytes
                - created_time: file creation timestamp
                - modified_time: file modification timestamp
        """
        self._sanitize_md_filenames(self.working_dir)
        md_files = list(self.working_dir.glob("*.md"))
        result = []
        for f in md_files:
            if f.is_file():
                stat = f.stat()
                result.append(
                    {
                        "filename": f.name,
                        "size": stat.st_size,
                        "path": str(f),
                        "created_time": datetime.fromtimestamp(
                            stat.st_ctime,
                        ).isoformat(),
                        "modified_time": datetime.fromtimestamp(
                            stat.st_mtime,
                        ).isoformat(),
                    },
                )
        return result

    def read_working_md(self, md_name: str) -> str:
        """Read markdown file content from the working directory.

        Returns:
            str: The file content as string
        """
        file_path = self._resolve_md_path(self.working_dir, md_name)
        if not file_path.exists():
            raise FileNotFoundError(f"Working md file not found: {md_name}")

        return read_text_file_with_encoding_fallback(file_path).strip()

    def write_working_md(self, md_name: str, content: str):
        """Write markdown content to a file in the working directory."""
        file_path = self._resolve_md_path(self.working_dir, md_name)
        file_path.write_text(content, encoding="utf-8")

    def append_working_md(self, md_name: str, content: str):
        """Append markdown content to a file in the working directory."""
        file_path = self._resolve_md_path(self.working_dir, md_name)
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(content)

    def list_memory_mds(self) -> list[dict]:
        """List all markdown files with metadata in the memory dir.

        Returns:
            list[dict]: A list of dictionaries, each containing:
                - filename: name of the file (with .md extension)
                - size: file size in bytes
                - created_time: file creation timestamp
                - modified_time: file modification timestamp
        """
        self._sanitize_md_filenames(self.memory_dir)
        md_files = list(self.memory_dir.glob("*.md"))
        result = []
        for f in md_files:
            if f.is_file():
                stat = f.stat()
                result.append(
                    {
                        "filename": f.name,
                        "size": stat.st_size,
                        "path": str(f),
                        "created_time": datetime.fromtimestamp(
                            stat.st_ctime,
                        ).isoformat(),
                        "modified_time": datetime.fromtimestamp(
                            stat.st_mtime,
                        ).isoformat(),
                    },
                )
        return result

    def read_memory_md(self, md_name: str) -> str:
        """Read markdown file content from the memory directory.

        Returns:
            str: The file content as string
        """
        file_path = self._resolve_md_path(self.memory_dir, md_name)
        if not file_path.exists():
            raise FileNotFoundError(f"Memory md file not found: {md_name}")

        return read_text_file_with_encoding_fallback(file_path).strip()

    def write_memory_md(self, md_name: str, content: str):
        """Write markdown content to a file in the memory directory."""
        file_path = self._resolve_md_path(self.memory_dir, md_name)
        file_path.write_text(content, encoding="utf-8")

    def append_memory_md(self, md_name: str, content: str):
        """Append markdown content to a file in the memory directory."""
        file_path = self._resolve_md_path(self.memory_dir, md_name)
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(content)
