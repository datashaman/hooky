"""Read/write/list/find/search file tools, mixed into ToolRuntime."""

from __future__ import annotations

import fnmatch

from pathlib import Path
from typing import Any

from hooky.runtime.evidence import content_sha256, file_sha256, relative_to
from hooky.runtime.git import matches_any_prefix
from hooky.runtime.text import apply_line_edits


class ReadWriteToolsMixin:
    """File read/write/list/find/search tool handlers."""

    def resolve_path(self, value: str) -> Path:
        path = (self.working_folder / value).resolve()
        if path != self.working_folder and self.working_folder not in path.parents:
            raise ValueError(f"path escapes working folder: {value}")
        return path

    def read_file_excerpt(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(str(args["path"]))
        self.validate_read_path(path)
        start_line = int(args.get("start_line") or 1)
        max_lines = int(args.get("max_lines") or 120)
        lines = path.read_text(encoding="utf-8").splitlines()
        start_index = max(start_line - 1, 0)
        excerpt = lines[start_index : start_index + max_lines]
        return {
            "ok": True,
            "path": relative_to(path, self.working_folder),
            "start_line": start_index + 1,
            "end_line": start_index + len(excerpt),
            "total_lines": len(lines),
            "content": "\n".join(excerpt),
            "truncated": start_index + max_lines < len(lines),
        }

    def read_files(self, args: dict[str, Any]) -> dict[str, Any]:
        max_bytes = int(args.get("max_bytes_per_file") or 12000)
        files = []
        for raw_path in list(args.get("paths") or [])[:50]:
            try:
                path = self.resolve_path(str(raw_path))
                self.validate_read_path(path)
                raw_bytes = path.read_bytes()
                full_content = raw_bytes.decode("utf-8")
                self.record_read_observation(path, full_content)
                content = raw_bytes[: max(1, max_bytes)].decode("utf-8", errors="replace")
                files.append(
                    {
                        "path": relative_to(path, self.working_folder),
                        "content": content,
                        "bytes": len(raw_bytes),
                        "truncated": len(raw_bytes) > len(content.encode("utf-8")),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - preserve batch reads when one file is absent.
                files.append({"path": str(raw_path), "error": str(exc)})
        return {"ok": True, "files": files, "truncated": len(list(args.get("paths") or [])) > 50}

    def is_read_blocked(self, path: Path) -> bool:
        relative = relative_to(path.resolve(), self.working_folder)
        parts = Path(relative).parts
        if matches_any_prefix(parts, self.read_allowed_prefixes):
            return False
        return matches_any_prefix(parts, self.read_blocked_prefixes)

    def validate_read_path(self, path: Path) -> None:
        if self.is_read_blocked(path):
            raise FileNotFoundError(f"path not found: {relative_to(path, self.working_folder)}")

    def write_files(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_files = list(args.get("files") or [])
        if not raw_files:
            raise ValueError("files is required")
        if len(raw_files) > 50:
            raise ValueError("write_files supports at most 50 files")
        prepared: list[tuple[Path, str]] = []
        for item in raw_files:
            if not isinstance(item, dict):
                raise ValueError("each file entry must be an object with path and content")
            path = self.resolve_path(str(item["path"]))
            content = str(item["content"])
            self.validate_write_path(path, "write_files")
            self.validate_write_has_current_read(path, "write_files")
            if self.write_validator:
                self.write_validator(path, content)
            prepared.append((path, content))
        for path, content in prepared:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            self.record_read_observation(path, content)
        files = [
            {
                "path": relative_to(path, self.working_folder),
                "bytes": path.stat().st_size,
            }
            for path, _content in prepared
        ]
        return {"ok": True, "files": files, "truncated": False}

    def edit_files(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_files = list(args.get("files") or [])
        if not raw_files:
            raise ValueError("files is required")
        if len(raw_files) > 50:
            raise ValueError("edit_files supports at most 50 files")
        prepared: list[tuple[Path, str, int]] = []
        for item in raw_files:
            if not isinstance(item, dict):
                raise ValueError("each file entry must be an object with path and edits")
            path = self.resolve_path(str(item["path"]))
            edits = list(item.get("edits") or [])
            if not edits:
                raise ValueError(f"edits are required: {relative_to(path, self.working_folder)}")
            self.validate_read_path(path)
            self.validate_write_path(path, "edit_files")
            if not path.exists():
                raise FileNotFoundError(f"path not found: {relative_to(path, self.working_folder)}")
            self.validate_write_has_current_read(path, "edit_files")
            original = path.read_text(encoding="utf-8")
            content = apply_line_edits(original, edits, relative_to(path, self.working_folder))
            if self.write_validator:
                self.write_validator(path, content)
            prepared.append((path, content, len(edits)))
        for path, content, _edit_count in prepared:
            path.write_text(content, encoding="utf-8")
            self.record_read_observation(path, content)
        files = [
            {
                "path": relative_to(path, self.working_folder),
                "bytes": path.stat().st_size,
                "edits": edit_count,
            }
            for path, _content, edit_count in prepared
        ]
        return {"ok": True, "files": files}

    def record_read_observation(self, path: Path, content: str) -> None:
        relative = relative_to(path.resolve(), self.working_folder)
        self.read_observations[relative] = {
            "generation": self.read_generation,
            "sha256": content_sha256(content),
            "size": len(content.encode("utf-8")),
        }

    def advance_read_generation(self) -> None:
        self.read_generation += 1
        self.read_observations.clear()

    def validate_write_has_current_read(self, path: Path, tool_name: str = "write_files") -> None:
        if not path.exists() or self.is_write_allowed_prefix_path(path):
            return
        relative = relative_to(path.resolve(), self.working_folder)
        observation = self.read_observations.get(relative)
        if not observation or observation.get("generation") != self.read_generation:
            raise ValueError(f"{tool_name} blocked: {relative} was not read in the current uncompacted context. Call read_files first.")
        current_hash = file_sha256(path)
        if current_hash != observation.get("sha256"):
            raise ValueError(f"{tool_name} blocked: {relative} changed since it was read. Call read_files again before writing.")

    def validate_write_path(self, path: Path, tool_name: str = "write_files") -> None:
        if not self.write_enabled:
            raise ValueError(f"{tool_name} is disabled for this agent; finish with final_report instead")
        relative = relative_to(path, self.working_folder)
        parts = Path(relative).parts
        if self.write_allowed_prefixes and not matches_any_prefix(parts, self.write_allowed_prefixes):
            raise ValueError(f"agent is not allowed to write outside allowed paths: {relative}")
        if matches_any_prefix(parts, self.write_blocked_prefixes):
            raise FileNotFoundError(f"path not found: {relative}")
        if path.name in set(self.write_blocked_names):
            raise ValueError(f"agent is not allowed to write system-managed file: {relative}")

    def is_write_allowed_prefix_path(self, path: Path) -> bool:
        if not self.write_allowed_prefixes:
            return False
        relative = relative_to(path.resolve(), self.working_folder)
        parts = Path(relative).parts
        return matches_any_prefix(parts, self.write_allowed_prefixes)

    def list_files(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(str(args.get("path") or "."))
        self.validate_read_path(path)
        entries = []
        for child in sorted(path.iterdir()):
            if self.is_read_blocked(child):
                continue
            entries.append({"path": relative_to(child, self.working_folder), "type": "dir" if child.is_dir() else "file"})
        return {"ok": True, "entries": entries}

    def find_files(self, args: dict[str, Any]) -> dict[str, Any]:
        root = self.resolve_path(str(args.get("path") or "."))
        self.validate_read_path(root)
        pattern = str(args["pattern"])
        matches = []
        for path in sorted(root.rglob("*")):
            relative = relative_to(path, self.working_folder)
            if (
                path.is_file()
                and not self.is_read_blocked(path)
                and (fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(relative, pattern))
            ):
                matches.append(relative)
        return {"ok": True, "matches": matches[:500], "truncated": len(matches) > 500}

    def search_files(self, args: dict[str, Any]) -> dict[str, Any]:
        root = self.resolve_path(str(args.get("path") or "."))
        self.validate_read_path(root)
        pattern = str(args["pattern"])
        matches = []
        for path in sorted(root.rglob("*")):
            if self.is_read_blocked(path) or not path.is_file() or path.stat().st_size > 1_000_000:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for index, line in enumerate(lines, 1):
                if pattern in line:
                    matches.append({"path": relative_to(path, self.working_folder), "line": index, "text": line[:500]})
                    if len(matches) >= 500:
                        return {"ok": True, "matches": matches, "truncated": True}
        return {"ok": True, "matches": matches, "truncated": False}

