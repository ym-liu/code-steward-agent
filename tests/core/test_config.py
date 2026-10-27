from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import load_config

EXAMPLE_CONFIG = Path(__file__).parents[2] / "config.example.yaml"


def write_config(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_config_is_valid():
    config = load_config(EXAMPLE_CONFIG)

    assert config.watched_folders


def test_defaults_fill_missing_settings(tmp_path):
    config = load_config(write_config(tmp_path, "watched_folders: [~/scripts]\n"))

    assert config.max_file_size_mb == 100
    assert ".git" in config.exclude_folders


def test_missing_watched_folder_is_allowed(tmp_path):
    config = load_config(write_config(tmp_path, "watched_folders: [~/does/not/exist]\n"))

    assert config.watched_folders == [Path.home() / "does/not/exist"]


def test_home_is_expanded(tmp_path):
    config = load_config(write_config(tmp_path, "watched_folders: [~/scripts]\n"))

    assert config.watched_folders == [Path.home() / "scripts"]


@pytest.mark.parametrize(
    "text",
    [
        "watched_folders: []\n",
        "exclude_folders: [.git]\n",
        "watched_folders: [~/scripts]\nwatched_folder: [typo]\n",
        "watched_folders: [~/scripts]\nmax_file_size_mb: big\n",
        "watched_folders: [~/scripts]\nmax_file_size_mb: 0\n",
        "watched_folders: [~/scripts]\nfull_scan_interval_days: 0\n",
        "watched_folders: [scripts]\n",
        "watched_folders: [~/scripts]\ndatabase_path: kamaji.sqlite3\n",
    ],
    ids=[
        "no watched folders",
        "watched folders missing",
        "unknown key",
        "wrong type",
        "size not positive",
        "interval not positive",
        "relative watched folder",
        "relative database path",
    ],
)
def test_invalid_config_is_rejected(tmp_path, text):
    with pytest.raises(ValidationError):
        load_config(write_config(tmp_path, text))


def test_missing_file_says_what_to_do(tmp_path):
    with pytest.raises(FileNotFoundError, match=r"config\.example\.yaml"):
        load_config(tmp_path / "config.yaml")
