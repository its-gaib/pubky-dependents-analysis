"""Main entry point for crate dependents analysis."""

import argparse
import json
import logging
import re
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from classify import (
    CategorizedEntry,
    Classification,
    RepoAnalysis,
    categorize,
    classify_cargo_toml,
    trace_chains,
)
from publication import validate_candidate
from sources import (
    RepoMatch,
    fetch_crates_io_downloads,
    fetch_crates_io_reverse_deps,
    fetch_file_content,
    fetch_github_stars,
    fetch_npm_downloads,
    scrape_github_dependents,
    search_github_cargo_lock,
    search_github_cargo_toml,
    search_npm_dependents,
)

log = logging.getLogger(__name__)

CLASSIFY_DELAY = 0.5  # seconds between repo classification API calls
STARS_DELAY = 0.3  # seconds between star-fetch API calls


def analyze_crate(
    crate_name: str,
    github_repo: str,
    npm_package: str | None = None,
    react_native_package: str | None = None,
    *,
    output_dir: str = "docs",
    run_id: str | None = None,
) -> str:
    """Run the full analysis pipeline for a single crate."""
    log.info("=== Analyzing %s (%s) ===", crate_name, github_repo)

    # Phase 1: Gather dependents from all sources
    log.info("Phase 1: Gathering dependents...")
    source_counts = {}
    all_repos = _gather_repos(crate_name, github_repo, source_counts)
    log.info("Total unique repos to classify: %d", len(all_repos))

    # Phase 2: Classify each repo
    log.info("Phase 2: Classifying repos...")
    classified = _classify_all(all_repos, crate_name)
    log.info("Classified %d repos", len(classified))

    # Phase 3: Fetch GitHub stars
    log.info("Phase 3: Fetching GitHub stars...")
    for i, repo_data in enumerate(classified):
        if (i + 1) % 20 == 0:
            log.info("  Fetching stars %d/%d...", i + 1, len(classified))
        repo_data.stars = fetch_github_stars(repo_data.repo)
        time.sleep(STARS_DELAY)

    # Phase 4: Categorize and output
    log.info("Phase 4: Categorizing and writing output...")
    categorized = categorize(classified, crate_name)

    npm_deps = []
    if npm_package:
        log.info("Searching npm dependents for %s...", npm_package)
        npm_deps = search_npm_dependents(npm_package)
        for source in ("npm_registry", "github_package_json"):
            source_counts[source] = sum(dep["source"] == source for dep in npm_deps)
        log.info("Found %d npm dependents", len(npm_deps))

    log.info("Fetching download counts...")
    downloads = fetch_crates_io_downloads(crate_name)
    npm_downloads = fetch_npm_downloads(npm_package) if npm_package else None
    rn_downloads = (
        fetch_npm_downloads(react_native_package) if react_native_package else None
    )

    output_path = _write_output(
        crate_name,
        categorized,
        npm_deps,
        downloads,
        npm_downloads,
        rn_downloads,
        output_dir=output_dir,
        collection={
            "status": "complete",
            "run_id": run_id or datetime.now(UTC).isoformat(),
            "sources": source_counts,
        },
    )
    log.info("Wrote %s", output_path)

    for list_name, entries in sorted(categorized.items()):
        log.info("  %s: %d repos", list_name, len(entries))

    return output_path


def _gather_repos(
    crate_name: str, github_repo: str, source_counts: dict | None = None
) -> dict[str, RepoMatch]:
    """Gather repos from all sources into a unified set."""
    log.info("  Fetching crates.io reverse deps...")
    crates_io_deps = fetch_crates_io_reverse_deps(crate_name)
    log.info("  Found %d crates.io reverse deps", len(crates_io_deps))

    log.info("  Searching GitHub Cargo.toml files...")
    toml_matches = search_github_cargo_toml(crate_name)
    log.info("  Found %d repos in Cargo.toml", len(toml_matches))

    log.info("  Searching GitHub Cargo.lock files...")
    lock_matches = search_github_cargo_lock(crate_name)
    log.info("  Found %d repos in Cargo.lock", len(lock_matches))

    log.info("  Scraping GitHub dependents page...")
    dependents = scrape_github_dependents(github_repo)
    log.info("  Found %d repos on dependents page", len(dependents))
    if source_counts is not None:
        source_counts.update(
            {
                "crates_io": len(crates_io_deps),
                "github_cargo_toml": len(toml_matches),
                "github_cargo_lock": len(lock_matches),
                "github_dependents": len(dependents),
            }
        )

    all_repos: dict[str, RepoMatch] = {}

    for match in toml_matches:
        all_repos[match.repo] = match

    for match in lock_matches:
        if match.repo in all_repos:
            all_repos[match.repo].cargo_lock_paths = match.cargo_lock_paths
        else:
            all_repos[match.repo] = match

    for dep_repo in dependents:
        if dep_repo not in all_repos:
            all_repos[dep_repo] = RepoMatch(repo=dep_repo, source="github_dependents")

    for dep in crates_io_deps:
        repo_url = dep.get("repository") or ""
        parsed = urlsplit(repo_url)
        if parsed.hostname == "github.com":
            repo_name = "/".join(parsed.path.strip("/").split("/")[:2]).removesuffix(
                ".git"
            )
            if (
                re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo_name)
                and repo_name not in all_repos
            ):
                all_repos[repo_name] = RepoMatch(repo=repo_name, source="crates_io")

    all_repos.pop(github_repo, None)
    return all_repos


def _classify_all(
    all_repos: dict[str, RepoMatch], target_crate: str
) -> list[RepoAnalysis]:
    """Classify every repo's relationship to the target crate."""
    classified = []
    for i, (repo_name, match) in enumerate(sorted(all_repos.items())):
        if (i + 1) % 10 == 0:
            log.info("  Processing %d/%d...", i + 1, len(all_repos))

        result = _classify_repo(repo_name, match, target_crate)
        if result:
            classified.append(result)

        time.sleep(CLASSIFY_DELAY)
    return classified


def _classify_repo(
    repo_name: str, match: RepoMatch, target_crate: str
) -> RepoAnalysis | None:
    """Classify a single repo's relationship to the target crate."""
    classification = None
    chain: list[str] = []

    # Try Cargo.toml first
    toml_paths = match.cargo_toml_paths or ["Cargo.toml"]
    for toml_path in toml_paths:
        content = fetch_file_content(repo_name, toml_path)
        if content:
            result = classify_cargo_toml(content, target_crate)
            if result and result.kind == "direct":
                classification = result
                crate_name = _extract_crate_name(content)
                chain = [crate_name or repo_name.split("/")[-1], target_crate]
                break
            elif result and result.kind == "feature_flag" and not classification:
                classification = result

    # If not direct, try Cargo.lock for chain tracing
    if not classification or classification.kind != "direct":
        lock_paths = match.cargo_lock_paths or ["Cargo.lock"]
        for lock_path in lock_paths:
            content = fetch_file_content(repo_name, lock_path)
            if content and target_crate in content:
                chains = trace_chains(content, target_crate)
                if chains:
                    chain = min(chains, key=len)
                    # Chain of length 2 means a root crate directly
                    # depends on target (no intermediary).
                    if len(chain) == 2 and not classification:
                        classification = Classification(kind="direct")
                    break

    if not classification and not chain:
        return None

    return RepoAnalysis(repo=repo_name, classification=classification, chain=chain)


def _extract_crate_name(toml_content: str) -> str | None:
    """Extract the [package] name from a Cargo.toml."""
    try:
        data = tomllib.loads(toml_content)
        return data.get("package", {}).get("name")
    except (tomllib.TOMLDecodeError, AttributeError):
        return None


def _write_output(
    crate_name: str,
    categorized: dict[str, list[CategorizedEntry]],
    npm_dependents: list[dict],
    downloads: dict | None,
    npm_downloads: dict | None,
    rn_downloads: dict | None = None,
    output_dir: str = "docs",
    collection: dict | None = None,
) -> str:
    """Write categorized dependents to a JSON file."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    serialized = {
        name: [e.to_dict() for e in entries] for name, entries in categorized.items()
    }

    summary = {name: len(entries) for name, entries in categorized.items()}
    total = sum(summary.values())

    output = {
        "crate": crate_name,
        "updated_at": datetime.now(UTC).isoformat(),
        "total": total,
        "summary": summary,
        "lists": serialized,
    }
    if collection is not None:
        output["collection"] = collection

    if downloads:
        output["crates_io_downloads"] = downloads

    if npm_downloads:
        output["npm_downloads"] = npm_downloads

    if rn_downloads:
        output["rn_downloads"] = rn_downloads

    if npm_dependents or (collection and "npm_registry" in collection["sources"]):
        output["npm_dependents"] = npm_dependents

    path = Path(output_dir) / f"{crate_name}.json"
    path.write_text(json.dumps(output, indent=2) + "\n")
    return str(path)


def main(argv: list[str] | None = None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )

    config_path = Path("crates.json")
    if not config_path.exists():
        log.error("crates.json not found")
        sys.exit(1)

    crates = json.loads(config_path.read_text())

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("crate", nargs="?", help="Analyze just this crate locally")
    parser.add_argument(
        "--allow-large-decrease",
        action="store_true",
        help="Accept an independently verified decrease; never bypasses source validation",
    )
    args = parser.parse_args(argv)
    selected = [c for c in crates if not args.crate or c["crate"] == args.crate]
    if not selected:
        parser.error("Unknown crate")
    run_id = datetime.now(UTC).isoformat()
    # Do not touch any published file unless every candidate has succeeded and
    # passed validation. Git's single commit then publishes the complete batch.
    with TemporaryDirectory(prefix="dependents-") as staging:
        paths = []
        for crate_config in selected:
            path = Path(
                analyze_crate(
                    crate_config["crate"],
                    crate_config["github_repo"],
                    npm_package=crate_config.get("npm_package"),
                    react_native_package=crate_config.get("react_native_package"),
                    output_dir=staging,
                    run_id=run_id,
                )
            )
            validate_candidate(
                path, Path("docs"), crate_config, args.allow_large_decrease
            )
            if json.loads(path.read_text())["collection"]["run_id"] != run_id:
                raise ValueError("Candidate belongs to a different analysis run")
            paths.append(path)
        Path("docs").mkdir(exist_ok=True)
        for path in paths:
            (Path("docs") / path.name).write_bytes(path.read_bytes())


if __name__ == "__main__":
    main()
