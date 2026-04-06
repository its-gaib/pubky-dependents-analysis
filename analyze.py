"""Main entry point for crate dependants analysis."""

import json
import sys
import time
from pathlib import Path

from classify import categorize, classify_cargo_toml, trace_chains
from output import write_output
from sources import (
    RepoMatch,
    fetch_crates_io_reverse_deps,
    fetch_file_content,
    fetch_github_stars,
    scrape_github_dependents,
    search_npm_dependents,
    search_github_cargo_lock,
    search_github_cargo_toml,
)


def analyze_crate(crate_name: str, github_repo: str, npm_package: str | None = None) -> str:
    """Run the full analysis pipeline for a single crate."""
    print(f"\n=== Analyzing {crate_name} ({github_repo}) ===")

    # Phase 1: Gather dependants from all sources
    print("Phase 1: Gathering dependants...")

    print("  Fetching crates.io reverse deps...")
    crates_io_deps = fetch_crates_io_reverse_deps(crate_name)
    print(f"  Found {len(crates_io_deps)} crates.io reverse deps")

    print("  Searching GitHub Cargo.toml files...")
    toml_matches = search_github_cargo_toml(crate_name)
    print(f"  Found {len(toml_matches)} repos with {crate_name} in Cargo.toml")

    print("  Searching GitHub Cargo.lock files...")
    lock_matches = search_github_cargo_lock(crate_name)
    print(f"  Found {len(lock_matches)} repos with {crate_name} in Cargo.lock")

    print("  Scraping GitHub dependents page...")
    dependents = scrape_github_dependents(github_repo)
    print(f"  Found {len(dependents)} repos on dependents page")

    npm_deps = []
    if npm_package:
        print(f"  Searching npm dependents for {npm_package}...")
        npm_deps = search_npm_dependents(npm_package)
        print(f"  Found {len(npm_deps)} npm dependents")

    # Merge all sources into a unified repo set
    all_repos: dict[str, RepoMatch] = {}

    for match in toml_matches:
        all_repos[match.repo] = match

    for match in lock_matches:
        if match.repo in all_repos:
            all_repos[match.repo].cargo_lock_paths = match.cargo_lock_paths
        else:
            all_repos[match.repo] = match

    # Add repos from dependents page that we haven't seen yet
    for dep_repo in dependents:
        if dep_repo not in all_repos:
            all_repos[dep_repo] = RepoMatch(repo=dep_repo, source="github_dependents")

    # Add repos from crates.io that have a repository URL
    for dep in crates_io_deps:
        repo_url = dep.get("repository") or ""
        if "github.com/" in repo_url:
            repo_name = repo_url.rstrip("/").split("github.com/")[-1]
            repo_name = repo_name.removesuffix(".git")
            if repo_name not in all_repos:
                all_repos[repo_name] = RepoMatch(repo=repo_name, source="crates_io")

    # Remove self
    all_repos.pop(github_repo, None)

    print(f"\nTotal unique repos to classify: {len(all_repos)}")

    # Phase 2: Classify each repo
    print("\nPhase 2: Classifying repos...")
    classified_repos = []

    for i, (repo_name, match) in enumerate(sorted(all_repos.items())):
        if (i + 1) % 10 == 0:
            print(f"  Processing {i + 1}/{len(all_repos)}...")

        repo_data = _classify_repo(repo_name, match, crate_name)
        if repo_data:
            classified_repos.append(repo_data)

        time.sleep(0.5)  # rate limiting

    print(f"  Classified {len(classified_repos)} repos")

    # Phase 3: Fetch GitHub stars
    print("\nPhase 3: Fetching GitHub stars...")
    for i, repo_data in enumerate(classified_repos):
        if (i + 1) % 20 == 0:
            print(f"  Fetching stars {i + 1}/{len(classified_repos)}...")
        stars = fetch_github_stars(repo_data["repo"])
        repo_data["stars"] = stars
        time.sleep(0.3)  # rate limiting

    # Phase 4: Categorize and output
    print("\nPhase 4: Categorizing and writing output...")
    categorized = categorize(classified_repos, crate_name)

    output_path = write_output(crate_name, categorized, npm_dependents=npm_deps)
    print(f"  Wrote {output_path}")

    # Print summary
    for list_name, entries in sorted(categorized.items()):
        print(f"  {list_name}: {len(entries)} repos")

    return output_path


def _classify_repo(repo_name: str, match: RepoMatch, target_crate: str) -> dict | None:
    """Classify a single repo's relationship to the target crate."""
    classification = None
    chain = []

    # Try Cargo.toml first
    if match.cargo_toml_paths:
        for toml_path in match.cargo_toml_paths:
            content = fetch_file_content(repo_name, toml_path)
            if content:
                result = classify_cargo_toml(content, target_crate)
                if result and result["kind"] == "direct":
                    classification = result
                    crate_name = _extract_crate_name(content)
                    chain = [crate_name or repo_name.split("/")[-1], target_crate]
                    break
                elif result and result["kind"] == "feature_flag" and not classification:
                    classification = result

    # If not direct, try Cargo.lock for chain tracing
    if not classification or classification["kind"] != "direct":
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

    return {
        "repo": repo_name,
        "classification": classification,
        "chain": chain,
    }


def _extract_crate_name(toml_content: str) -> str | None:
    """Extract the [package] name from a Cargo.toml."""
    import tomllib
    try:
        data = tomllib.loads(toml_content)
        return data.get("package", {}).get("name")
    except Exception:
        return None


def main():
    config_path = Path("crates.json")
    if not config_path.exists():
        print("Error: crates.json not found")
        sys.exit(1)

    crates = json.loads(config_path.read_text())

    # Optional: filter to a single crate via CLI arg
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
