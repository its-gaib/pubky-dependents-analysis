"""Validate the configuration and JSON snapshots served by the dashboard."""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from publication import validate_snapshot

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = sorted((ROOT / "docs").glob("*.json"))
REPO_PATTERN = r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+"


def assert_count(value):
    assert type(value) is int and value >= 0, f"Invalid count: {value!r}"


def test_configured_crates_have_snapshots():
    crates = json.loads((ROOT / "crates.json").read_text())
    assert isinstance(crates, list) and crates
    names = []
    for config in crates:
        name = config["crate"]
        assert re.fullmatch(r"[A-Za-z0-9_-]+", name)
        assert re.fullmatch(REPO_PATTERN, config["github_repo"])
        for field in ("npm_package", "react_native_package"):
            if field in config:
                assert isinstance(config[field], str) and config[field]
        assert (ROOT / "docs" / f"{name}.json").is_file()
        names.append(name)
    assert len(names) == len(set(names)), "Duplicate configured crates"


def test_new_publications_contain_one_complete_run_for_all_configured_crates():
    crates = json.loads((ROOT / "crates.json").read_text())
    snapshots = [
        json.loads((ROOT / "docs" / f"{config['crate']}.json").read_text())
        for config in crates
    ]
    if not any("collection" in data for data in snapshots):
        return  # Existing legacy exports remain readable until the first new run.
    for config, data in zip(crates, snapshots, strict=True):
        validate_snapshot(data, config)
    assert len({data["collection"]["run_id"] for data in snapshots}) == 1


@pytest.mark.parametrize("path", SNAPSHOTS, ids=lambda path: path.name)
def test_published_snapshot_is_consistent(path):
    # Historical exports remain valid API endpoints even after a crate is removed.
    data = json.loads(path.read_text())
    assert data["crate"] == path.stem
    updated = datetime.fromisoformat(data["updated_at"])
    assert updated.tzinfo is not None, "updated_at must include a timezone"
    assert_count(data["total"])
    assert isinstance(data["summary"], dict)
    assert isinstance(data["lists"], dict)
    assert data["summary"].keys() == data["lists"].keys()
    assert data["total"] == sum(data["summary"].values())

    repos = []
    for category, count in data["summary"].items():
        assert category
        assert_count(count)
        entries = data["lists"][category]
        assert isinstance(entries, list)
        assert len(entries) == count, f"Incorrect count for {category}"
        for entry in entries:
            assert re.fullmatch(REPO_PATTERN, entry["repo"])
            repos.append(entry["repo"].lower())
            assert isinstance(entry["chain"], list)
            assert all(isinstance(crate, str) and crate for crate in entry["chain"])
            if entry["chain"]:
                assert entry["chain"][-1] == data["crate"]
            if entry.get("stars") is not None:
                assert_count(entry["stars"])
            if "version" in entry:
                assert isinstance(entry["version"], str)
            if "features" in entry:
                assert isinstance(entry["features"], list)
                assert all(isinstance(feature, str) for feature in entry["features"])
            for field in ("optional", "default_features"):
                if field in entry:
                    assert isinstance(entry[field], bool)
    assert len(repos) == len(set(repos)), "A repo is counted more than once"

    for field in ("crates_io_downloads", "npm_downloads", "rn_downloads"):
        if field in data:
            assert isinstance(data[field], dict)
            assert_count(data[field]["total"])
            assert_count(data[field]["recent"])

    if "npm_dependents" in data:
        assert isinstance(data["npm_dependents"], list)
        for dependent in data["npm_dependents"]:
            for field in ("package", "source"):
                assert isinstance(dependent[field], str)
            assert dependent["package"]
            if dependent.get("description") is not None:
                assert isinstance(dependent["description"], str)

    if "collection" in data:
        configs = json.loads((ROOT / "crates.json").read_text())
        config = next(
            (config for config in configs if config["crate"] == data["crate"]),
            {"crate": data["crate"]},
        )
        validate_snapshot(data, config)
