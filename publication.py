"""Validate complete candidates and decide when a weekly recovery is needed."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

CORE_SOURCES = {
    "crates_io",
    "github_cargo_toml",
    "github_cargo_lock",
    "github_dependents",
}
NPM_SOURCES = {"npm_registry", "github_package_json"}


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("Counts must be nonnegative integers")


def validate_snapshot(data: dict, config: dict) -> None:
    """Validate counts and provenance without claiming complete index coverage."""
    if data["crate"] != config["crate"]:
        raise ValueError("Snapshot crate does not match configuration")
    collection = data["collection"]
    if collection["status"] != "complete":
        raise ValueError("Only completed collections can be published")
    measured = datetime.fromisoformat(data["updated_at"])
    started = datetime.fromisoformat(collection["run_id"])
    if (
        measured.utcoffset() != timedelta(0)
        or started.utcoffset() != timedelta(0)
        or not started <= measured <= datetime.now(UTC) + timedelta(minutes=5)
    ):
        raise ValueError("Invalid measurement timestamp or run identity")
    sources = collection["sources"]
    required = CORE_SOURCES | (NPM_SOURCES if config.get("npm_package") else set())
    if not required <= sources.keys():
        raise ValueError("Collection is missing a required source")
    for count in sources.values():
        _count(count)
    _count(data["total"])
    if data["summary"].keys() != data["lists"].keys():
        raise ValueError("Category lists and summary disagree")
    repos = []
    for category, count in data["summary"].items():
        _count(count)
        if count != len(data["lists"][category]):
            raise ValueError("Category count and entries disagree")
        repos.extend(entry["repo"].lower() for entry in data["lists"][category])
    if data["total"] != sum(data["summary"].values()) or len(repos) != len(set(repos)):
        raise ValueError("Snapshot totals disagree or contain duplicate repositories")
    npm = (
        data["npm_dependents"]
        if config.get("npm_package")
        else data.get("npm_dependents", [])
    )
    if not isinstance(npm, list):
        raise TypeError("npm_dependents must be an array")
    if config.get("npm_package") and sum(
        sources[source] for source in NPM_SOURCES
    ) != len(npm):
        raise ValueError("npm source counts and entries disagree")


def validate_candidate(
    path: Path, previous_dir: Path, config: dict, allow_decrease=False
) -> None:
    data = json.loads(path.read_text())
    validate_snapshot(data, config)
    previous_path = previous_dir / path.name
    if allow_decrease or not previous_path.exists():
        return
    previous = json.loads(previous_path.read_text())
    checks = {
        "classified repositories": (previous["total"], data["total"]),
        "npm sample": (
            len(previous.get("npm_dependents", [])),
            len(data.get("npm_dependents", [])),
        ),
    }
    for source, before in previous.get("collection", {}).get("sources", {}).items():
        if source in data["collection"]["sources"]:
            checks[source] = (before, data["collection"]["sources"][source])
    for source, (before, after) in checks.items():
        if before - after >= 20 and after < before * 0.75:
            raise ValueError(
                f"Suspicious decrease for {data['crate']} {source}: {before} -> {after}. "
                "Last publication preserved; investigate before --allow-large-decrease."
            )


def recovery_due(docs: Path, crates: list[dict], now: datetime) -> bool:
    """The Monday 06:00 cycle gets at most three later scheduled attempts."""
    cycle = (now - timedelta(days=now.weekday())).replace(
        hour=6, minute=0, second=0, microsecond=0
    )
    if now < cycle:
        cycle -= timedelta(days=7)
    run_ids = set()
    try:
        for config in crates:
            data = json.loads((docs / f"{config['crate']}.json").read_text())
            validate_snapshot(data, config)
            run_id = data["collection"]["run_id"]
            if datetime.fromisoformat(run_id) < cycle:
                return True
            run_ids.add(run_id)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return True
    return len(run_ids) != 1


if __name__ == "__main__":
    crates = json.loads(Path("crates.json").read_text())
    print(f"due={str(recovery_due(Path('docs'), crates, datetime.now(UTC))).lower()}")
