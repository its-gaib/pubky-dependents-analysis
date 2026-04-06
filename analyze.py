"""Main entry point for crate dependants analysis."""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from classify import (
    CategorizedEntry,
    Classification,
    RepoAnalysis,
    categorize,
    classify_cargo_toml,
    trace_chains,
)
from sources import (
    RepoMatch,
    fetch_crates_io_reverse_deps,
    fetch_file_content,
    fetch_github_stars,
    scrape_github_dependents,
    search_github_cargo_lock,
    search_github_cargo_toml,
    search_npm_dependents,
)

log = logging.getLogger(__name__)

CLASSIFY_DELAY = 0.5  # seconds between repo classification API calls
STARS_DELAY = 0.3  # seconds between star-fetch API calls


def analyze_crate(crate_name: str, github_repo: str, npm_package: str | None = None) -> str:
    """Run the full analysis pipeline for a single crate."""
    log.info("=== Analyzing %s (%s) ===", crate_name, github_repo)

    # Phase 1: Gather dependants from all sources
    log.info("Phase 1: Gathering dependants...")
    all_repos = _gather_repos(crate_name, github_repo)
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
        log.info("Found %d npm dependents", len(npm_deps))

    output_path = _write_output(crate_name, categorized, npm_deps)
    log.info("Wrote %s", output_path)

    for list_name, entries in sorted(categorized.items()):
        log.info("  %s: %d repos", list_name, len(entries))

    return output_path


def _gather_repos(crate_name: str, github_repo: str) -> dict[str, RepoMatch]:
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
        if "github.com/" in repo_url:
            repo_name = repo_url.rstrip("/").split("github.com/")[-1]
            repo_name = repo_name.removesuffix(".git")
            if repo_name not in all_repos:
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
    if match.cargo_toml_paths:
        for toml_path in match.cargo_toml_paths:
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
                    break

    if not classification and not chain:
        return None

    return RepoAnalysis(
        repo=repo_name, classification=classification, chain=chain
    )


def _extract_crate_name(toml_content: str) -> str | None:
    """Extract the [package] name from a Cargo.toml."""
    import tomllib
    try:
        data = tomllib.loads(toml_content)
        return data.get("package", {}).get("name")
    except Exception:
        return None


def _write_output(
    crate_name: str,
    categorized: dict[str, list[CategorizedEntry]],
    npm_dependents: list[dict],
    output_dir: str = "docs",
) -> str:
    """Write categorized dependants to a JSON file."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    serialized = {
        name: [e.to_dict() for e in entries]
        for name, entries in categorized.items()
    }

    summary = {name: len(entries) for name, entries in categorized.items()}
    total = sum(summary.values())

    output = {
        "crate": crate_name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "summary": summary,
        "lists": serialized,
    }

    if npm_dependents:
        output["npm_dependents"] = npm_dependents

    path = Path(output_dir) / f"{crate_name}.json"
    path.write_text(json.dumps(output, indent=2) + "\n")
    return str(path)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )

    config_path = Path("crates.json")
    if not config_path.exists():
        log.error("crates.json not found")
        sys.exit(1)

    crates = json.loads(config_path.read_text())

    filter_crate = sys.argv[1] if len(sys.argv) > 1 else None

    for crate_config in crates:
        if filter_crate and crate_config["crate"] != filter_crate:
            continue
        analyze_crate(
            crate_config["crate"],
            crate_config["github_repo"],
            npm_package=crate_config.get("npm_package"),
        )


if __name__ == "__main__":
    main()
