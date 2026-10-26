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
GH_SEARCH_DELAY = 15  # leave headroom below code search's 10 requests/minute limit
GH_RATE_LIMIT_BACKOFF = 60  # minimum secondary-limit backoff; doubles each retry
GH_RATE_LIMIT_MAX_BACKOFF = 600  # cap backoff at 10 minutes
GH_RATE_LIMIT_WAIT_BUDGET = 3660  # total retry wait per request, including reset hints
GH_MAX_ATTEMPTS = 10
GH_SEARCH_MAX_RESULTS = 1000
GH_SEARCH_MAX_ATTEMPTS = 3
GH_SEARCH_RETRY_DELAY = 15  # whole-search restart delay; doubles each retry


class SourceError(RuntimeError):
    """A required source could not be collected completely."""


class _TransientSearchError(SourceError):
    """Search pagination could not yield a stable, complete snapshot."""


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


def _gh_response(result: subprocess.CompletedProcess) -> tuple[int | None, dict, str]:
    """Separate gh api --include headers from either JSON or --jq output."""
    output = result.stdout or ""
    status = None
    headers = {}
    while True:
        parts = re.split(r"\r?\n\r?\n", output, maxsplit=1)
        lines = parts[0].splitlines()
        match = (
            re.fullmatch(r"HTTP/[\d.]+ ([1-5][0-9]{2})(?: .*)?", lines[0])
            if lines
            else None
        )
        if len(parts) != 2 or not match:
            break
        status = int(match.group(1))
        headers = {}
        for line in lines[1:]:
            name, colon, value = line.partition(":")
            if colon:
                headers[name.strip().lower()] = value.strip()
        output = parts[1]
    if status is None:
        match = re.search(r"\bHTTP ([1-5][0-9]{2})\b", result.stderr or "")
        if match:
            status = int(match.group(1))
    return status, headers, output


def _is_gh_rate_limited(result: subprocess.CompletedProcess) -> bool:
    """Recognize rate-limit status/messages, never arbitrary response numbers."""
    status, headers, body = _gh_response(result)
    if status == 429:
        return True
    if status not in (None, 403):
        return False
    if status == 403 and headers.get("x-ratelimit-remaining") == "0":
        return True
    message = result.stderr or ""
    try:
        data = json.loads(body)
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            message += "\n" + data["message"]
    except (ValueError, TypeError):
        pass
    return any(
        marker in message.lower()
        for marker in ("rate limit exceeded", "secondary rate limit", "abuse detection")
    )


def _gh_rate_limit_category(result: subprocess.CompletedProcess) -> str:
    """Classify upstream errors without exposing their potentially sensitive text."""
    _, headers, body = _gh_response(result)
    if headers.get("x-ratelimit-remaining") == "0":
        return "primary"
    message = result.stderr or ""
    try:
        data = json.loads(body)
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            message += "\n" + data["message"]
    except (ValueError, TypeError):
        pass
    if any(
        marker in message.lower()
        for marker in ("secondary rate limit", "abuse detection")
    ):
        return "secondary"
    return "unknown"


def _gh_diagnostics(
    category: str,
    status: int | None,
    headers: dict,
    *,
    rate_limit: str | None = None,
    search_context: dict | None = None,
) -> str:
    """Emit fixed categories, validated search context, and numeric metadata."""
    fields = [category, f"status={status if status is not None else 'unknown'}"]
    if rate_limit in ("primary", "secondary", "unknown"):
        fields.append(f"rate_limit={rate_limit}")
    if search_context is not None:
        fields.append("endpoint=search/code")
        query = search_context.get("search_query")
        if (
            isinstance(query, str)
            and len(query) <= 100
            and re.fullmatch(r"(?:@[A-Za-z0-9_-]+/)?[A-Za-z0-9_.-]+", query)
        ):
            fields.append(f"search_query={query}")
        filename = search_context.get("search_filename")
        if filename in ("Cargo.toml", "Cargo.lock", "package.json"):
            fields.append(f"search_filename={filename}")
        page = search_context.get("search_page")
        if type(page) is int and 1 <= page <= 1000:
            fields.append(f"search_page={page}")
    for name in (
        "retry-after",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-used",
        "x-ratelimit-reset",
    ):
        value = headers.get(name, "")
        if re.fullmatch(r"[0-9]{1,16}", value):
            fields.append(f"{name}={int(value)}")
    return " ".join(fields)


def _gh_retry_delay(headers: dict, attempt: int, remaining_wait: float) -> float:
    """Honor server delays while enforcing a total wait budget for each request."""
    delay = min(GH_RATE_LIMIT_BACKOFF * (2**attempt), GH_RATE_LIMIT_MAX_BACKOFF)
    for name in ("retry-after", "x-ratelimit-reset"):
        if name not in headers:
            continue
        if name == "x-ratelimit-reset" and headers.get("x-ratelimit-remaining") != "0":
            continue
        if not re.fullmatch(r"[0-9]{1,16}", headers[name]):
            raise SourceError("GitHub rate limit has invalid retry timing metadata")
        wait = int(headers[name])
        if name == "x-ratelimit-reset":
            wait = max(0, wait - time.time() + 1)
        delay = max(delay, wait)
    if delay > remaining_wait:
        raise SourceError("GitHub retry delay exceeds the request wait budget")
    return delay


def _run_gh(
    cmd: list[str],
    *,
    timeout: int = 60,
    pre_delay: float = 0,
    allow_not_found: bool = False,
    search_context: dict | None = None,
) -> subprocess.CompletedProcess | None:
    """Run a fixed-argument gh command with bounded rate-limit/timeout retries."""
    if cmd[:2] == ["gh", "api"] and "--include" not in cmd:
        cmd = [*cmd[:2], "--include", *cmd[2:]]
    if pre_delay > 0:
        time.sleep(pre_delay)

    waited = 0
    for attempt in range(GH_MAX_ATTEMPTS):
        headers = {}
        status = None
        category = "timeout"
        rate_limit = None
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
            status, headers, body = _gh_response(result)
            if result.returncode == 0:
                result.stdout = body
                return result
            if allow_not_found and status == 404:
                return None
            if not _is_gh_rate_limited(result):
                diagnostics = _gh_diagnostics(
                    "request_failed", status, headers, search_context=search_context
                )
                raise SourceError(
                    f"GitHub request failed; refusing partial results ({diagnostics})"
                )
            category = "rate_limited"
            rate_limit = _gh_rate_limit_category(result)
        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError as exc:
            raise SourceError("gh CLI not found") from exc

        diagnostics = _gh_diagnostics(
            category,
            status,
            headers,
            rate_limit=rate_limit,
            search_context=search_context,
        )
        if attempt < GH_MAX_ATTEMPTS - 1:
            try:
                backoff = _gh_retry_delay(
                    headers, attempt, GH_RATE_LIMIT_WAIT_BUDGET - waited
                )
            except SourceError as exc:
                raise SourceError(f"{exc} ({diagnostics})") from None
            log.warning(
                "GitHub request %s (attempt %d/%d); retry in %.1fs",
                diagnostics,
                attempt + 1,
                GH_MAX_ATTEMPTS,
                backoff,
            )
            time.sleep(backoff)
            waited += backoff
    raise SourceError(
        f"GitHub request failed after {GH_MAX_ATTEMPTS} attempts ({diagnostics})"
    )


def _search_items(
    query: str, filename: str, *, sample_limit: int | None = None
) -> list[dict]:
    """Retry unstable searches from page one, never retaining partial results."""
    for attempt in range(GH_SEARCH_MAX_ATTEMPTS):
        try:
            return _search_items_once(query, filename, sample_limit=sample_limit)
        except _TransientSearchError as exc:
            if attempt == GH_SEARCH_MAX_ATTEMPTS - 1:
                raise SourceError(
                    f"Incomplete GitHub search after {GH_SEARCH_MAX_ATTEMPTS} attempts "
                    f"for {query} in {filename} ({exc})"
                ) from None
            delay = GH_SEARCH_RETRY_DELAY * (2**attempt)
            log.warning(
                "GitHub search %s (attempt %d/%d); restart from page 1 in %ds",
                exc,
                attempt + 1,
                GH_SEARCH_MAX_ATTEMPTS,
                delay,
            )
            time.sleep(delay)


def _search_items_once(
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
            search_context={
                "search_query": query,
                "search_filename": filename,
                "search_page": page,
            },
        )
        try:
            data = json.loads(result.stdout)
            total = data["total_count"]
            page_items = data["items"]
            if (
                type(total) is not int
                or total < 0
                or type(data.get("incomplete_results")) is not bool
                or not isinstance(page_items, list)
            ):
                raise ValueError("malformed response")
            if sample_limit is None and total >= GH_SEARCH_MAX_RESULTS:
                raise SourceError(
                    f"GitHub search for {query} in {filename} hit the 1000-result API cap; split the query"
                )
            if data["incomplete_results"]:
                raise _TransientSearchError(f"incomplete_results page={page}")
            if expected_total is not None and total != expected_total:
                raise _TransientSearchError(
                    f"changing_total page={page} expected={expected_total} received={total}"
                )
            expected_total = total
            wanted = min(total, sample_limit) if sample_limit else total
            if len(page_items) != min(per_page, wanted - len(items)):
                raise _TransientSearchError(
                    f"truncated_page page={page} "
                    f"expected={min(per_page, wanted - len(items))} received={len(page_items)}"
                )
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
                    raise _TransientSearchError(f"duplicate_result page={page}")
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
