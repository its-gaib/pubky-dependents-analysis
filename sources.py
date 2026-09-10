"""Fetch dependent data from crates.io, GitHub search, and GitHub dependents page."""

import base64
import html as html_mod
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urljoin, urlsplit

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
GH_MAX_ATTEMPTS = 4
GH_SEARCH_MAX_RESULTS = 1000


class SourceError(RuntimeError):
    """A required source could not be collected completely."""


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
    expected_total = None
    while True:
        resp = requests.get(
            f"{CRATES_IO_BASE}/crates/{crate_name}/reverse_dependencies",
            params={"per_page": 100, "page": page},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        versions = data.get("versions")
        total = data.get("meta", {}).get("total")
        if (
            not isinstance(versions, list)
            or type(total) is not int
            or not 0 <= total <= 10000
            or (page > 1 and total != expected_total)
            or len(versions) != min(100, total - len(results))
        ):
            raise SourceError(
                f"Incomplete crates.io reverse dependencies for {crate_name}"
            )
        expected_total = total
        for version in versions:
            results.append(
                {
                    "crate": version.get("crate", version.get("num", "")),
                    "version": version.get("num", ""),
                    "description": version.get("description", ""),
                    "repository": version.get("repository", ""),
                }
            )

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
    cmd: list[str],
    *,
    timeout: int = 60,
    pre_delay: float = 0,
    allow_not_found: bool = False,
) -> subprocess.CompletedProcess | None:
    """Run a fixed-argument gh command with bounded rate-limit/timeout retries."""
    if pre_delay > 0:
        time.sleep(pre_delay)

    for attempt in range(GH_MAX_ATTEMPTS):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
            if result.returncode == 0:
                return result
            if allow_not_found and "HTTP 404" in (result.stderr or ""):
                return None
            if not _is_gh_rate_limited(result):
                raise SourceError("GitHub request failed; refusing partial results")
        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError as exc:
            raise SourceError("gh CLI not found") from exc

        if attempt < GH_MAX_ATTEMPTS - 1:
            backoff = min(
                GH_RATE_LIMIT_BACKOFF * (2**attempt),
                GH_RATE_LIMIT_MAX_BACKOFF,
            )
            log.warning(
                "GitHub request rate limited or timed out (attempt %d/%d); retry in %ds",
                attempt + 1,
                GH_MAX_ATTEMPTS,
                backoff,
            )
            time.sleep(backoff)
    raise SourceError(f"GitHub request failed after {GH_MAX_ATTEMPTS} attempts")


def _search_items(
    query: str, filename: str, *, sample_limit: int | None = None
) -> list[dict]:
    """Keep GitHub's completeness metadata and verify every requested page.

    npm intentionally retains its historical first-50 sample; Rust requires all
    matches. A complete response is not a guarantee of GitHub index coverage.
    """
    items = []
    expected_total = None
    per_page = sample_limit or 100
    page = 1
    seen = set()
    while True:
        result = _run_gh(
            [
                "gh",
                "api",
                "--hostname",
                "github.com",
                "--method",
                "GET",
                "search/code",
                "-f",
                f"q={query} filename:{filename}",
                "-f",
                f"per_page={per_page}",
                "-f",
                f"page={page}",
            ],
            pre_delay=GH_SEARCH_DELAY,
        )
        try:
            data = json.loads(result.stdout)
            total = data["total_count"]
            page_items = data["items"]
            if (
                type(total) is not int
                or total < 0
                or data.get("incomplete_results") is not False
                or not isinstance(page_items, list)
            ):
                raise ValueError("incomplete or malformed response")
            if sample_limit is None and total >= GH_SEARCH_MAX_RESULTS:
                raise SourceError(
                    f"GitHub search for {query} in {filename} hit the 1000-result API cap; split the query"
                )
            if expected_total is not None and total != expected_total:
                raise ValueError("result count changed during pagination")
            expected_total = total
            wanted = min(total, sample_limit) if sample_limit else total
            if len(page_items) != min(per_page, wanted - len(items)):
                raise ValueError("truncated search page")
            for item in page_items:
                repo = item["repository"]["full_name"]
                path = item["path"]
                if (
                    not isinstance(repo, str)
                    or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo)
                    or not isinstance(path, str)
                    or not path
                ):
                    raise ValueError("invalid repository or path")
                identity = (repo, path)
                if identity in seen:
                    raise ValueError("duplicate search result across pages")
                seen.add(identity)
            items.extend(page_items)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise SourceError(
                f"Incomplete GitHub search for {query} in {filename}"
            ) from exc
        if len(items) == wanted:
            return items
        page += 1


def _gh_search_code(query: str, filename: str, path_attr: str) -> list[RepoMatch]:
    """Return repository matches only after all search pages are verified."""
    items = _search_items(query, filename)
    repo_map: dict[str, RepoMatch] = {}
    for item in items:
        repo = item["repository"]["full_name"]
        path = item["path"]
        if repo not in repo_map:
            repo_map[repo] = RepoMatch(repo=repo, source=f"github_{filename}")
        getattr(repo_map[repo], path_attr).append(path)

    return list(repo_map.values())


def scrape_github_dependents(github_repo: str) -> list[str]:
    """Scrape the GitHub dependents page, following all pagination."""
    url = f"https://github.com/{github_repo}/network/dependents"
    return _scrape_dependents_pages(url, github_repo)


def _fetch_dependents_page(url: str) -> str:
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
    raise SourceError("GitHub dependents page unavailable after bounded retries")


def _scrape_dependents_pages(start_url: str, github_repo: str) -> list[str]:
    """Paginate through all dependents pages starting from a URL."""
    repos: list[str] = []
    url = start_url
    seen_pages = set()

    while True:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or parsed.path != f"/{github_repo}/network/dependents"
            or url in seen_pages
            or len(seen_pages) >= 100
        ):
            raise SourceError("Invalid or looping GitHub dependents pagination")
        seen_pages.add(url)
        html = _fetch_dependents_page(url)
        if 'content="dependents"' not in html:
            raise SourceError("GitHub returned an unrecognized dependents page")
        matches = list(
            re.finditer(
                r'<a[^>]+data-hovercard-type="repository"[^>]+href="/([^"]+)"',
                html,
            )
        )
        for match in matches:
            repo = match.group(1)
            if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo):
                raise SourceError(
                    "GitHub dependents page contains an invalid repository"
                )
            if repo != github_repo and repo not in repos:
                repos.append(repo)
        if not matches and "No repositories found" not in html:
            raise SourceError("GitHub dependents page has no recognizable results")

        # Find next page link
        next_match = re.search(
            r'<a[^>]*class="[^"]*"[^>]*href="([^"]+)"[^>]*>Next</a>', html
        )
        if not next_match:
            break
        url = urljoin(url, html_mod.unescape(next_match.group(1)))

        time.sleep(SCRAPE_DELAY)

    return repos


def fetch_file_content(repo: str, path: str) -> str | None:
    """Fetch a file from a GitHub repo via the API."""
    result = _run_gh(
        [
            "gh",
            "api",
            "--hostname",
            "github.com",
            f"repos/{repo}/contents/{quote(path, safe='/')}",
            "--jq",
            ".content",
        ],
        timeout=30,
        allow_not_found=True,
    )
    if result is None:
        return _fetch_raw(repo, path)

    try:
        encoded = "".join(result.stdout.split())
        return base64.b64decode(encoded, validate=True).decode(
            "utf-8", errors="replace"
        )
    except ValueError:
        return _fetch_raw(repo, path)


def fetch_github_stars(repo: str) -> int | None:
    """Fetch the star count for a GitHub repo."""
    try:
        result = _run_gh(
            [
                "gh",
                "api",
                "--hostname",
                "github.com",
                f"repos/{repo}",
                "--jq",
                ".stargazers_count",
            ],
            timeout=15,
        )
    except SourceError:
        return None
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
        resp.raise_for_status()
        data = resp.json()
        total = data.get("total")
        objects = data.get("objects")
        if (
            type(total) is not int
            or total < 0
            or not isinstance(objects, list)
            or len(objects) != min(50, total)
        ):
            raise SourceError("Incomplete npm registry search")
        for obj in objects:
            pkg = obj["package"]
            name = pkg["name"]
            if not isinstance(name, str) or not name:
                raise ValueError("Invalid npm package name")
            if name != package_name and name not in seen:
                seen.add(name)
                dependents.append(
                    {
                        "package": name,
                        "description": pkg.get("description", ""),
                        "source": "npm_registry",
                    }
                )
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise SourceError("npm registry search failed") from exc

    # Source 2: GitHub code search for package.json references
    for item in _search_items(package_name, "package.json", sample_limit=50):
        repo = item["repository"]["full_name"]
        if repo not in seen:
            seen.add(repo)
            dependents.append({"package": repo, "source": "github_package_json"})

    return dependents


def _fetch_raw(repo: str, path: str) -> str | None:
    """Fetch raw file content from GitHub."""
    for branch in ("main", "master", "develop"):
        try:
            resp = requests.get(
                f"https://raw.githubusercontent.com/{repo}/{branch}/{quote(path, safe='/')}",
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.text
            if resp.status_code != 404:
                resp.raise_for_status()
        except requests.RequestException as exc:
            raise SourceError("GitHub file content unavailable") from exc
    return None
