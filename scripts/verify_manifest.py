#!/usr/bin/env python3
"""Verify release-file hashes and, in a worktree, manifest completeness."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from pathlib import Path
from typing import AbstractSet


_LINE_RE = re.compile(r"([0-9a-f]{64})  (\./[^\r\n]+)")


class ManifestError(RuntimeError):
    """Raised when a release manifest is malformed or does not match disk."""


def _safe_relative_path(raw: str) -> str:
    if not raw.startswith("./") or "\\" in raw:
        raise ManifestError(f"unsafe manifest path: {raw!r}")
    relative = raw[2:]
    parts = relative.split("/")
    if not relative or any(part in {"", ".", ".."} for part in parts):
        raise ManifestError(f"unsafe manifest path: {raw!r}")
    return relative


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_tracked_paths(root: Path) -> set[str] | None:
    if not (root / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        decoded = completed.stdout.decode("utf-8")
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        raise ManifestError("could not enumerate the git index") from exc
    return {item for item in decoded.split("\0") if item != "MANIFEST.sha256" and item}


def verify_manifest(
    root: Path,
    *,
    expected_paths: AbstractSet[str] | None = None,
) -> int:
    root = root.resolve()
    manifest_path = root / "MANIFEST.sha256"
    try:
        lines = manifest_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ManifestError("could not read MANIFEST.sha256") from exc
    if not lines:
        raise ManifestError("MANIFEST.sha256 is empty")

    seen: set[str] = set()
    entries: list[tuple[str, str]] = []
    for line_number, line in enumerate(lines, 1):
        match = _LINE_RE.fullmatch(line)
        if match is None:
            raise ManifestError(f"malformed manifest line {line_number}")
        expected_hash, raw_path = match.groups()
        relative = _safe_relative_path(raw_path)
        if relative in seen:
            raise ManifestError(f"duplicate manifest path: {relative}")
        seen.add(relative)
        entries.append((expected_hash, relative))

    for expected_hash, relative in entries:
        candidate = root.joinpath(*relative.split("/"))
        current = root
        for part in relative.split("/"):
            current /= part
            if current.is_symlink():
                raise ManifestError(f"manifest path contains a symlink: {relative}")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ManifestError(f"missing or unsafe manifest file: {relative}") from exc
        if not resolved.is_file():
            raise ManifestError(f"manifest path is not a regular file: {relative}")
        if _sha256(resolved) != expected_hash:
            raise ManifestError(f"hash mismatch: {relative}")

    required = set(expected_paths) if expected_paths is not None else _git_tracked_paths(root)
    if required is not None:
        missing = sorted(required - seen)
        unexpected = sorted(seen - required)
        if missing or unexpected:
            details: list[str] = []
            if missing:
                details.append("unlisted tracked files: " + ", ".join(missing[:5]))
            if unexpected:
                details.append("untracked manifest paths: " + ", ".join(unexpected[:5]))
            raise ManifestError("; ".join(details))
    return len(seen)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Release root containing MANIFEST.sha256",
    )
    args = parser.parse_args()
    try:
        count = verify_manifest(args.root)
    except ManifestError as exc:
        parser.error(str(exc))
    print(f"Manifest verification passed: {count} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
