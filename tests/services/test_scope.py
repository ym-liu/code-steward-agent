import os
from pathlib import Path

import pytest

from app.services.rules.scope import PROTECTED_FOLDERS, is_in_scope

# Only the protected folders that are full paths on this OS, e.g. C:/Windows on Windows, /bin elsewhere.
LOCAL_PROTECTED_FOLDERS = [folder for folder in PROTECTED_FOLDERS if folder.is_absolute()]


def in_scope(path, watched_folders, deny_paths=(), ignore=(".git/",)):
    return is_in_scope(path, list(watched_folders), list(deny_paths), list(ignore))


# Check: Inside a watched folder


def test_file_in_watched_folder_is_in_scope(tmp_path):
    assert in_scope(tmp_path / "tools" / "backup.py", [tmp_path])


def test_file_outside_watched_folders_is_out_of_scope(tmp_path):
    assert not in_scope(tmp_path / "other" / "backup.py", [tmp_path / "scripts"])


# Check: Not protected


@pytest.mark.parametrize("folder", LOCAL_PROTECTED_FOLDERS, ids=str)
def test_protected_folder_is_out_of_scope(folder):
    assert not in_scope(folder / "tool.py", [Path(folder.anchor)])


def test_denied_folder_is_out_of_scope(tmp_path):
    assert not in_scope(tmp_path / "secret" / "keys.py", [tmp_path], deny_paths=[tmp_path / "secret"])


@pytest.mark.parametrize("folder", ["$RECYCLE.BIN", "System Volume Information", "lost+found"])
def test_protected_pattern_is_out_of_scope(tmp_path, folder):
    assert not in_scope(tmp_path / folder / "old.py", [tmp_path])


def test_config_cannot_unprotect(tmp_path):
    assert not in_scope(tmp_path / "$RECYCLE.BIN" / "old.py", [tmp_path], ignore=["!$RECYCLE.BIN/"])


# Check: Not ignored


def test_ignored_folder_is_out_of_scope_at_any_depth(tmp_path):
    assert not in_scope(tmp_path / "project" / ".git" / "hooks" / "pre-commit", [tmp_path])


def test_ignored_name_above_the_watched_folder_does_not_count(tmp_path):
    root = tmp_path / ".git" / "scripts"

    assert in_scope(root / "backup.py", [root])


def test_folder_pattern_keeps_a_file_with_that_name(tmp_path):
    assert in_scope(tmp_path / "tools" / "build", [tmp_path], ignore=["build/"])


@pytest.mark.parametrize(
    ("path", "ignore"),
    [
        (Path("logs") / "run.log", ["*.log"]),
        (Path("mypackage.egg-info") / "PKG-INFO", ["*.egg-info/"]),
    ],
    ids=["file pattern", "folder glob"],
)
def test_ignore_patterns(tmp_path, path, ignore):
    assert not in_scope(tmp_path / path, [tmp_path], ignore=ignore)


@pytest.mark.parametrize("name", ["~$report.docx", "notes.txt.swp", "setup.exe.part", "draft.py~"])
def test_temp_file_is_out_of_scope(tmp_path, name):
    assert not in_scope(tmp_path / name, [tmp_path], ignore=[])


@pytest.mark.skipif(os.name != "nt", reason="only Windows ignores case")
@pytest.mark.parametrize(
    "path",
    [Path(".GIT") / "config", Path("$Recycle.Bin") / "old.py", Path("SETUP.TMP")],
    ids=str,
)
def test_windows_ignores_case(tmp_path, path):
    assert not in_scope(tmp_path / path, [tmp_path])
