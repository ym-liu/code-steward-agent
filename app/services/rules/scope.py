import os
from functools import cache
from pathlib import Path

from pathspec import GitIgnoreSpec

# System folders, never read. Full paths.
PROTECTED_FOLDERS = [
    Path("C:/Windows"),
    Path("C:/Program Files"),
    Path("C:/Program Files (x86)"),
    Path("C:/ProgramData"),
    Path("/bin"),
    Path("/sbin"),
    Path("/usr"),
    Path("/etc"),
    Path("/dev"),
    Path("/proc"),
    Path("/sys"),
    Path("/System"),
    Path("/Library"),
]

# System folders at the top of every drive, never read.
PROTECTED_PATTERNS = [
    "$RECYCLE.BIN/",
    "System Volume Information/",
    ".Trashes/",
    ".Spotlight-V100/",
    ".fseventsd/",
    "lost+found/",
]

# Editor and download leftovers, skipped.
TEMP_PATTERNS = [
    "~$*",
    ".~lock.*",
    "*.tmp",
    "*.swp",
    "*.swx",
    "*.part",
    "*.crdownload",
    "*~",
]


def is_in_scope(
    path: Path,
    watched_folders: list[Path],
    deny_paths: list[Path],
    ignore: list[str],
) -> bool:
    """Should this path be scanned?"""
    # Check: Inside a watched folder
    root = _find_parent(path, watched_folders)
    if root is None:
        return False

    relative_path = path.relative_to(root)

    # Check: Not protected
    if _find_parent(path, PROTECTED_FOLDERS + deny_paths) is not None:
        return False
    if _matches_patterns(relative_path, PROTECTED_PATTERNS):
        return False

    # Check: Not ignored
    return not _matches_patterns(relative_path, TEMP_PATTERNS + ignore)


def _find_parent(path: Path, folders: list[Path]) -> Path | None:
    return next((folder for folder in folders if path.is_relative_to(folder)), None)


def _matches_patterns(relative_path: Path, patterns: list[str]) -> bool:
    spec = _compile_patterns(tuple(patterns))
    return spec.match_file(_normalize_case(relative_path.as_posix()))


@cache
def _compile_patterns(patterns: tuple[str, ...]) -> GitIgnoreSpec:
    return GitIgnoreSpec.from_lines(_normalize_case(pattern) for pattern in patterns)


def _normalize_case(text: str) -> str:
    # pathspec is case-sensitive, Windows isn't
    return text.lower() if os.name == "nt" else text
