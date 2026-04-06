"""Fetch dependant data from crates.io, GitHub search, and GitHub dependents page."""

import json
import re
import subprocess
import time
from dataclasses import dataclass, field

import requests

USER_AGENT = "pubky-dependants-analysis (https://github.com/its-gaib/pubky-dependants-analysis)"
CRATES_IO_BASE = "https://crates.io/api/v1"


@dataclass
class RepoMatch:
    """A repository that references the target crate."""
    repo: str  # owner/name
    cargo_toml_paths: list[str] = field(default_factory=list)
    cargo_lock_paths: list[str] = field(default_factory=list)
    source: str = ""  # where we found it


def fetch_crates_io_reverse_deps(crate_name: str) -> list[dict]:
    """Fetch all published crates that depend on target crate from crates.io."""
    results = []
    page = 1
    while True:
        resp = requests.get(
            f"{CRATES_IO_BASE}/crates/{crate_name}/reverse_dependencies",
            params={"per_page": 100, "page": page},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        for version in data.get("versions", []):
            results.append({
                "crate": version.get("crate", version.get("num", "")),
                "version": version.get("num", ""),
                "description": version.get("description", ""),
                "repository": version.get("repository", ""),
            })

        total = data.get("meta", {}).get("total", 0)
        if page * 100 >= total:
            break
        page += 1
        time.sleep(1)  # crates.io rate limit

    return results


def search_github_cargo_toml(crate_name: str) -> list[RepoMatch]:
    """Search GitHub for repos that mention the crate in Cargo.toml files."""
    return _gh_search_code(crate_name, "Cargo.toml", "cargo_toml_paths")


def search_github_cargo_lock(crate_name: str) -> list[RepoMatch]:
    """Search GitHub for repos that mention the crate in Cargo.lock files."""
    return _gh_search_code(crate_name, "Cargo.lock", "cargo_lock_paths")


def _gh_search_code(query: str, filename: str, path_attr: str) -> list[RepoMatch]:
    """Run gh search code and return RepoMatch objects."""
    try:
        result = subprocess.run(
            [
                "gh", "search", "code", query,
                "--filename", filename,
                "--limit", "100",
                "--json", "repository,path",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"Warning: gh search failed: {result.stderr}")
            return []

        items = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        print(f"Warning: gh search error: {e}")
        return []

    repo_map: dict[str, RepoMatch] = {}
    for item in items:
        repo = item["repository"]["nameWithOwner"]
        path = item["path"]
        if repo not in repo_map:
            repo_map[repo] = RepoMatch(repo=repo, source=f"github_{filename}")
        getattr(repo_map[repo], path_attr).append(path)

    return list(repo_map.values())


def scrape_github_dependents(github_repo: str) -> list[str]:
    """Scrape the GitHub dependents page to get repo names.

    GitHub doesn't have an API for this, so we parse the HTML.
    """
    repos = []
    url = f"https://github.com/{github_repo}/network/dependents"

    for _page in range(10):  # limit to 10 pages
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"Warning: dependents page fetch failed: {e}")
            break

        html = resp.text

        # Extract repo names from the dependents page
        # Pattern: <a data-hovercard-type="repository" ...>owner/repo</a>
        # or: <a href="/owner/repo">repo</a> within the dependents list
        for match in re.finditer(
            r'data-repository-id="\d+"[^>]*>\s*'
            r'<a[^>]*href="/([^"]+)"[^>]*>([^<]+)</a>\s*'
            r'</span>\s*<span[^>]*>\s*/'
            r'\s*</span>\s*<a[^>]*href="/([^"]+)"[^>]*>([^<]+)</a>',
            html,
        ):
            owner = match.group(2).strip()
            name = match.group(4).strip()
            repos.append(f"{owner}/{name}")

        # Also try a simpler pattern
        if not repos or _page > 0:
            for match in re.finditer(
                r'<a[^>]+data-hovercard-type="repository"[^>]+href="/([^"]+)"',
                html,
            ):
                repo = match.group(1)
                if repo != github_repo and repo not in repos:
                    repos.append(repo)

        # Find next page link
        next_match = re.search(r'<a[^>]*class="[^"]*"[^>]*href="([^"]+)"[^>]*>Next</a>', html)
        if not next_match:
            break
        url = next_match.group(1)
        if not url.startswith("http"):
            url = f"https://github.com{url}"
        time.sleep(1)

    return list(dict.fromkeys(repos))  # dedupe preserving order


def fetch_file_content(repo: str, path: str) -> str | None:
    """Fetch a file from a GitHub repo via the API."""
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/contents/{path}", "--jq", ".content"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            # Try raw URL for large files
            return _fetch_raw(repo, path)

        import base64
        return base64.b64decode(result.stdout.strip()).decode("utf-8", errors="replace")
    except Exception:
        return _fetch_raw(repo, path)


def fetch_github_stars(repo: str) -> int | None:
    """Fetch the star count for a GitHub repo."""
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}", "--jq", ".stargazers_count"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return int(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def search_npm_dependents(package_name: str) -> list[dict]:
    """Search for npm packages that depend on the target package.

    Uses npm registry search API and GitHub code search for package.json.
    """
    dependents = []
    seen = set()

    # Source 1: npm registry search
    try:
        resp = requests.get(
            "https://registry.npmjs.org/-/v1/search",
            params={"text": package_name, "size": 50},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            for obj in data.get("objects", []):
                pkg = obj["package"]
                name = pkg["name"]
                if name != package_name and name not in seen:
                    seen.add(name)
                    dependents.append({
                        "package": name,
                        "description": pkg.get("description", ""),
                        "source": "npm_registry",
                    })
    except requests.RequestException as e:
        print(f"Warning: npm registry search failed: {e}")

    # Source 2: GitHub code search for package.json references
    try:
        result = subprocess.run(
            [
                "gh", "search", "code", package_name,
                "--filename", "package.json",
                "--limit", "50",
                "--json", "repository,path",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            items = json.loads(result.stdout)
            for item in items:
                repo = item["repository"]["nameWithOwner"]
                if repo not in seen:
                    seen.add(repo)
                    dependents.append({
                        "package": repo,
                        "source": "github_package_json",
                    })
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        print(f"Warning: gh search for package.json failed: {e}")

    return dependents


def _fetch_raw(repo: str, path: str) -> str | None:
    """Fetch raw file content from GitHub."""
    for branch in ("main", "master", "develop"):
        try:
            resp = requests.get(
                f"https://raw.githubusercontent.com/{repo}/{branch}/{path}",
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.text
        except requests.RequestException:
            continue
    return None
