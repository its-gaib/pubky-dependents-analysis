"""Fetch dependent data from crates.io, GitHub search, and GitHub dependents page."""

import base64
import html as html_mod
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "pubky-dependents-analysis (https://github.com/its-gaib/pubky-dependents-analysis)"
)
CRATES_IO_BASE = "https://crates.io/api/v1"
CRATES_IO_DELAY = 1  # seconds between crates.io requests
SCRAPE_DELAY = 2  # seconds between dependents page requests
SCRAPE_MAX_RETRIES = 3  # retries per page on failure
GH_SEARCH_DELAY = 5  # seconds to wait before each gh search code call
GH_RATE_LIMIT_BACKOFF = 30  # initial backoff seconds on rate limit, doubles each retry
GH_RATE_LIMIT_MAX_BACKOFF = 600  # cap backoff at 10 minutes


@dataclass
class RepoMatch:
    """A repository that references the target crate."""

    repo: str  # owner/name
    cargo_toml_paths: list[str] = field(default_factory=list)
    cargo_lock_paths: list[str] = field(default_factory=list)
    source: str = ""  # where we found it


def fetch_crates_io_downloads(crate_name: str) -> dict | None:
    """Fetch download counts for a crate from crates.io."""
    try:
        resp = requests.get(
            f"{CRATES_IO_BASE}/crates/{crate_name}",
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        crate = resp.json().get("crate", {})
        return {
            "total": crate.get("downloads", 0),
            "recent": crate.get("recent_downloads", 0),
        }
    except requests.RequestException:
        return None


def fetch_npm_downloads(package_name: str) -> dict | None:
    """Fetch download counts for an npm package."""
    try:
        # Last 30 days
        resp = requests.get(
            f"https://api.npmjs.org/downloads/point/last-month/{package_name}",
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        recent = resp.json().get("downloads", 0)

        # All-time (wide date range)
        resp = requests.get(
            f"https://api.npmjs.org/downloads/range/2000-01-01:2099-01-01/{package_name}",
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        if resp.status_code != 200:
            return {"total": 0, "recent": recent}
        total = sum(d["downloads"] for d in resp.json().get("downloads", []))

        return {"total": total, "recent": recent}
    except requests.RequestException:
        return None


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
        if resp.status_code == 404:
            log.warning("Crate %s not found on crates.io", crate_name)
            return results
        resp.raise_for_status()
        data = resp.json()

        for version in data.get("versions", []):
            results.append(
                {
                    "crate": version.get("crate", version.get("num", "")),
                    "version": version.get("num", ""),
                    "description": version.get("description", ""),
                    "repository": version.get("repository", ""),
                }
            )

        total = data.get("meta", {}).get("total", 0)
        if page * 100 >= total:
            break
        page += 1
        time.sleep(CRATES_IO_DELAY)

    return results


def search_github_cargo_toml(crate_name: str) -> list[RepoMatch]:
    """Search GitHub for repos that mention the crate in Cargo.toml files."""
    return _gh_search_code(crate_name, "Cargo.toml", "cargo_toml_paths")


def search_github_cargo_lock(crate_name: str) -> list[RepoMatch]:
    """Search GitHub for repos that mention the crate in Cargo.lock files."""
    return _gh_search_code(crate_name, "Cargo.lock", "cargo_lock_paths")


def _is_gh_rate_limited(result: subprocess.CompletedProcess) -> bool:
    """Check if a gh CLI result indicates a rate limit (HTTP 429 or 403 abuse)."""
    combined = ((result.stderr or "") + (result.stdout or "")).lower()
    return "429" in combined or "abuse" in combined or "rate limit" in combined


def _run_gh(
    cmd: list[str], *, timeout: int = 60, pre_delay: float = 0
) -> subprocess.CompletedProcess | None:
    """Run a gh CLI command, retrying indefinitely on rate limits.

    Returns the successful CompletedProcess, or None on non-rate-limit errors.
    """
    if pre_delay > 0:
        time.sleep(pre_delay)

    attempt = 0
    while True:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
            if result.returncode == 0:
                return result
            if _is_gh_rate_limited(result):
                attempt += 1
                backoff = min(
                    GH_RATE_LIMIT_BACKOFF * (2 ** (attempt - 1)),
                    GH_RATE_LIMIT_MAX_BACKOFF,
                )
                log.warning(
                    "gh rate limited (attempt %d), retrying in %ds...",
                    attempt,
                    backoff,
                )
                time.sleep(backoff)
                continue
            log.debug("gh command failed: %s", result.stderr.strip())
            return None
        except subprocess.TimeoutExpired:
            attempt += 1
            backoff = min(
                GH_RATE_LIMIT_BACKOFF * (2 ** (attempt - 1)),
                GH_RATE_LIMIT_MAX_BACKOFF,
            )
            log.warning(
                "gh command timed out (attempt %d), retrying in %ds...",
                attempt,
                backoff,
            )
            time.sleep(backoff)
            continue
        except FileNotFoundError:
            log.warning("gh CLI not found")
            return None


def _gh_search_code(query: str, filename: str, path_attr: str) -> list[RepoMatch]:
    """Run gh search code and return RepoMatch objects, with retry on rate limit."""
    cmd = [
        "gh",
        "search",
        "code",
        query,
        "--filename",
        filename,
        "--limit",
        "1000",
        "--json",
        "repository,path",
    ]

    result = _run_gh(cmd, timeout=60, pre_delay=GH_SEARCH_DELAY)
    if result is None:
        return []

    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        log.warning("gh search JSON parse error: %s", e)
        return []

    if len(items) >= 1000:
        log.warning(
            "gh search for %s in %s hit the 1000-result API cap — results may be incomplete",
            query,
            filename,
        )

    repo_map: dict[str, RepoMatch] = {}
    for item in items:
        repo = item["repository"]["nameWithOwner"]
        path = item["path"]
        if repo not in repo_map:
            repo_map[repo] = RepoMatch(repo=repo, source=f"github_{filename}")
        getattr(repo_map[repo], path_attr).append(path)

    return list(repo_map.values())


def scrape_github_dependents(github_repo: str) -> list[str]:
    """Scrape the GitHub dependents page, following all pagination."""
    url = f"https://github.com/{github_repo}/network/dependents"
    return _scrape_dependents_pages(url, github_repo)


def _fetch_dependents_page(url: str) -> str | None:
    """Fetch a single dependents page with retries."""
    for attempt in range(SCRAPE_MAX_RETRIES):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            if attempt < SCRAPE_MAX_RETRIES - 1:
                delay = SCRAPE_DELAY * (attempt + 1)
                log.warning(
                    "Dependents page fetch failed (attempt %d): %s — retrying in %ds",
                    attempt + 1,
                    e,
                    delay,
                )
                time.sleep(delay)
            else:
                log.warning(
                    "Dependents page fetch failed after %d attempts: %s",
                    SCRAPE_MAX_RETRIES,
                    e,
                )
    return None


def _scrape_dependents_pages(start_url: str, github_repo: str) -> list[str]:
    """Paginate through all dependents pages starting from a URL."""
    repos: list[str] = []
    url = start_url

    while True:
        html = _fetch_dependents_page(url)
        if html is None:
            break

        for match in re.finditer(
            r'<a[^>]+data-hovercard-type="repository"[^>]+href="/([^"]+)"',
            html,
        ):
            repo = match.group(1)
            if repo != github_repo and repo not in repos:
                repos.append(repo)

        # Find next page link
        next_match = re.search(
            r'<a[^>]*class="[^"]*"[^>]*href="([^"]+)"[^>]*>Next</a>', html
        )
        if not next_match:
            break
        url = html_mod.unescape(next_match.group(1))
        if not url.startswith("http"):
            url = f"https://github.com{url}"

        time.sleep(SCRAPE_DELAY)

    return repos


def fetch_file_content(repo: str, path: str) -> str | None:
    """Fetch a file from a GitHub repo via the API."""
    result = _run_gh(
        ["gh", "api", f"repos/{repo}/contents/{path}", "--jq", ".content"],
        timeout=30,
    )
    if result is None:
        return _fetch_raw(repo, path)

    try:
        return base64.b64decode(result.stdout.strip()).decode("utf-8", errors="replace")
    except ValueError:
        return _fetch_raw(repo, path)


def fetch_github_stars(repo: str) -> int | None:
    """Fetch the star count for a GitHub repo."""
    result = _run_gh(
        ["gh", "api", f"repos/{repo}", "--jq", ".stargazers_count"],
        timeout=15,
    )
    if result is not None and result.stdout.strip().isdigit():
        return int(result.stdout.strip())
    return None


def search_npm_dependents(package_name: str) -> list[dict]:
    """Search for npm packages that reference the target package."""
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
                    dependents.append(
                        {
                            "package": name,
                            "description": pkg.get("description", ""),
                            "source": "npm_registry",
                        }
                    )
    except requests.RequestException as e:
        log.warning("npm registry search failed: %s", e)

    # Source 2: GitHub code search for package.json references
    result = _run_gh(
        [
            "gh",
            "search",
            "code",
            package_name,
            "--filename",
            "package.json",
            "--limit",
            "50",
            "--json",
            "repository,path",
        ],
        timeout=60,
        pre_delay=GH_SEARCH_DELAY,
    )
    if result is not None:
        try:
            items = json.loads(result.stdout)
            for item in items:
                repo = item["repository"]["nameWithOwner"]
                if repo not in seen:
                    seen.add(repo)
                    dependents.append(
                        {
                            "package": repo,
                            "source": "github_package_json",
                        }
                    )
        except json.JSONDecodeError as e:
            log.warning("gh search for package.json JSON parse error: %s", e)

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
