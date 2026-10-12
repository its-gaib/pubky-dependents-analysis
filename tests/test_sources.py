"""Source adapter behavior with no HTTP requests, subprocesses, or real waits."""

import base64
import json
import logging
import subprocess
from unittest.mock import Mock

import pytest
import requests

import sources


@pytest.fixture(autouse=True)
def no_real_waits(monkeypatch):
    monkeypatch.setattr(sources.time, "sleep", Mock())


def _response(status, payload=None):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode()
    return response


def _gh_response(status, body="", headers=None, *, newline="\r\n"):
    header_lines = [f"HTTP/2.0 {status} Test"]
    header_lines.extend(f"{key}: {value}" for key, value in (headers or {}).items())
    output = newline.join(header_lines) + newline * 2 + body
    return subprocess.CompletedProcess([], 0 if status < 400 else 1, output, "")


def test_gh_retries_rate_limits_and_timeouts_with_capped_backoff(monkeypatch):
    command = ["gh", "api", "repos/example/repo"]
    limited = subprocess.CompletedProcess(command, 1, "", "HTTP 429: rate limit")
    success = subprocess.CompletedProcess(command, 0, "42\n", "")
    run = Mock(
        side_effect=[
            limited,
            subprocess.TimeoutExpired(command, 15),
            *([limited] * 5),
            success,
        ]
    )
    sleep = Mock()
    monkeypatch.setattr(sources.subprocess, "run", run)
    monkeypatch.setattr(sources.time, "sleep", sleep)
    monkeypatch.setattr(sources, "GH_MAX_ATTEMPTS", 8)

    assert sources._run_gh(command, timeout=15, pre_delay=5) is success

    assert [call.args[0] for call in sleep.call_args_list] == [
        5,
        60,
        120,
        240,
        480,
        600,
        600,
        600,
    ]
    assert run.call_count == 8
    actual_command = run.call_args.args[0]
    assert "--include" in actual_command
    assert [arg for arg in actual_command if arg != "--include"] == command
    run.assert_called_with(
        actual_command, capture_output=True, text=True, timeout=15, check=False
    )


def test_gh_default_pacing_respects_code_search_and_secondary_limits():
    assert sources.GH_SEARCH_DELAY >= 6
    assert sources.GH_RATE_LIMIT_BACKOFF >= 60
    assert sources.GH_MAX_ATTEMPTS == 4
    assert sources.GH_SEARCH_MAX_ATTEMPTS == 3


@pytest.mark.parametrize("newline", ["\r\n", "\n"])
def test_gh_include_headers_do_not_corrupt_search_json(monkeypatch, newline):
    payload = {
        "total_count": 1,
        "incomplete_results": False,
        "items": _items(1),
    }
    run = Mock(
        return_value=_gh_response(
            200,
            json.dumps(payload),
            {"X-RateLimit-Remaining": "9"},
            newline=newline,
        )
    )
    monkeypatch.setattr(sources.subprocess, "run", run)

    matches = sources.search_github_cargo_lock("pkarr")

    assert [match.repo for match in matches] == ["example/repo0"]
    assert run.call_args.args[0].count("--include") == 1


@pytest.mark.parametrize("newline", ["\r\n", "\n"])
def test_gh_include_headers_do_not_corrupt_jq_output(monkeypatch, newline):
    content = '[package]\nname = "example"\n'
    encoded = base64.b64encode(content.encode()).decode()
    run = Mock(
        side_effect=[
            _gh_response(200, encoded + "\n", newline=newline),
            _gh_response(200, "42\n", newline=newline),
        ]
    )
    raw = Mock(side_effect=AssertionError("valid API content must not fall back"))
    monkeypatch.setattr(sources.subprocess, "run", run)
    monkeypatch.setattr(sources, "_fetch_raw", raw)

    assert sources.fetch_file_content("example/repo", "Cargo.toml") == content
    assert sources.fetch_github_stars("example/repo") == 42
    assert all("--include" in call.args[0] for call in run.call_args_list)
    raw.assert_not_called()


@pytest.mark.parametrize(
    ("status", "headers", "expected_delay"),
    [
        (429, {"retry-after": "121"}, 121),
        (429, {"Retry-After": "10"}, 60),
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1900"}, 1801),
        (429, {"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": "1900"}, 60),
        (
            403,
            {
                "Retry-After": "1900",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1900",
            },
            1900,
        ),
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3700"}, 3601),
    ],
)
def test_gh_waits_for_server_rate_limit_deadline(
    monkeypatch, status, headers, expected_delay
):
    run = Mock(
        side_effect=[_gh_response(status, headers=headers), _gh_response(200, "ok")]
    )
    sleep = Mock()
    monkeypatch.setattr(sources.subprocess, "run", run)
    monkeypatch.setattr(sources.time, "sleep", sleep)
    monkeypatch.setattr(sources.time, "time", Mock(return_value=100))

    assert sources._run_gh(["gh", "api", "repos/example/repo"]).stdout == "ok"

    sleep.assert_called_once_with(expected_delay)
    assert run.call_count == 2


@pytest.mark.parametrize(
    "headers",
    [
        {"Retry-After": "4000"},
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4100"},
        {"Retry-After": "-1"},
        {"Retry-After": "not-a-delay"},
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "invalid"},
    ],
)
def test_gh_rejects_waits_it_cannot_honor_without_retrying_early(monkeypatch, headers):
    run = Mock(return_value=_gh_response(429, headers=headers))
    sleep = Mock()
    monkeypatch.setattr(sources.subprocess, "run", run)
    monkeypatch.setattr(sources.time, "sleep", sleep)
    monkeypatch.setattr(sources.time, "time", Mock(return_value=100))

    with pytest.raises(sources.SourceError):
        sources._run_gh(["gh", "api", "repos/example/repo"])

    run.assert_called_once()
    sleep.assert_not_called()


def test_gh_total_wait_budget_stops_instead_of_shortening_server_delay(monkeypatch):
    run = Mock(return_value=_gh_response(429, headers={"Retry-After": "2000"}))
    sleep = Mock()
    monkeypatch.setattr(sources.subprocess, "run", run)
    monkeypatch.setattr(sources.time, "sleep", sleep)

    with pytest.raises(sources.SourceError):
        sources._run_gh(["gh", "api", "repos/example/repo"])

    assert run.call_count == 2
    sleep.assert_called_once_with(2000)


def test_gh_rate_limit_diagnostics_never_expose_response_secrets(monkeypatch, caplog):
    secret = "secret-response-token-123"
    response = _gh_response(
        429,
        json.dumps({"message": "rate limit", "secret": secret}),
        {"Retry-After": "60", "X-Secret": secret},
    )
    response.stderr = f"HTTP 429: rate limited; credential={secret}"
    monkeypatch.setattr(sources.subprocess, "run", Mock(return_value=response))
    caplog.set_level(logging.WARNING, logger="sources")

    with pytest.raises(sources.SourceError) as error:
        sources._run_gh(["gh", "api", "repos/example/repo"])

    assert "429" in caplog.text
    assert secret not in caplog.text
    assert secret not in str(error.value)


def test_gh_arbitrary_body_429_does_not_turn_forbidden_into_rate_limit(monkeypatch):
    response = _gh_response(403, json.dumps({"message": "resource 429 is private"}))
    response.stderr = "HTTP 403: Forbidden"
    run = Mock(return_value=response)
    sleep = Mock()
    monkeypatch.setattr(sources.subprocess, "run", run)
    monkeypatch.setattr(sources.time, "sleep", sleep)

    with pytest.raises(sources.SourceError):
        sources._run_gh(["gh", "api", "repos/example/repo"])

    run.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("failure", ["not_found", "forbidden", "invalid_json"])
def test_gh_search_errors_fail_without_publishing_zero(monkeypatch, failure):
    results = {
        "not_found": FileNotFoundError("gh is not installed"),
        "forbidden": subprocess.CompletedProcess([], 1, "", "HTTP 403: Forbidden"),
        "invalid_json": subprocess.CompletedProcess([], 0, "not JSON", ""),
    }
    run = Mock(side_effect=[results[failure]])
    sleep = Mock()
    monkeypatch.setattr(sources.subprocess, "run", run)
    monkeypatch.setattr(sources.time, "sleep", sleep)

    with pytest.raises(sources.SourceError):
        sources.search_github_cargo_toml("pkarr")

    run.assert_called_once()
    sleep.assert_called_once_with(sources.GH_SEARCH_DELAY)


@pytest.mark.parametrize(
    ("search", "path_attribute", "filename"),
    [
        (sources.search_github_cargo_toml, "cargo_toml_paths", "Cargo.toml"),
        (sources.search_github_cargo_lock, "cargo_lock_paths", "Cargo.lock"),
    ],
)
def test_gh_search_groups_files_by_repository(
    monkeypatch, search, path_attribute, filename
):
    items = [
        {"repository": {"full_name": repo}, "path": path}
        for repo, path in [
            ("example/shared", filename),
            ("example/shared", f"workspace/{filename}"),
            ("example/other", filename),
        ]
    ]
    payload = {"total_count": len(items), "incomplete_results": False, "items": items}
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), ""))
    monkeypatch.setattr(sources.subprocess, "run", run)
    monkeypatch.setattr(sources.time, "sleep", Mock())

    matches = {match.repo: match for match in search("pkarr")}

    assert set(matches) == {"example/shared", "example/other"}
    assert getattr(matches["example/shared"], path_attribute) == [
        filename,
        f"workspace/{filename}",
    ]
    assert getattr(matches["example/other"], path_attribute) == [filename]
    assert f"q=pkarr filename:{filename}" in run.call_args.args[0]


@pytest.mark.parametrize("content", ["invalid base64", "非ASCII"])
def test_invalid_gh_file_content_falls_back_to_raw(monkeypatch, content):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, content, ""))
    raw = Mock(return_value='[package]\nname = "example"\n')
    monkeypatch.setattr(sources.subprocess, "run", run)
    monkeypatch.setattr(sources, "_fetch_raw", raw)

    assert sources.fetch_file_content("example/repo", "Cargo.toml") == raw.return_value

    raw.assert_called_once_with("example/repo", "Cargo.toml")


def test_reverse_dependencies_follow_pagination(monkeypatch):
    get = Mock(
        side_effect=[
            _response(
                200,
                {
                    "versions": [
                        {
                            "crate": "first",
                            "num": "1.0",
                            "repository": "https://github.com/example/first",
                        },
                        *[{"crate": f"other{i}"} for i in range(99)],
                    ],
                    "meta": {"total": 101},
                },
            ),
            _response(
                200,
                {"versions": [{"crate": "last", "num": "2.0"}], "meta": {"total": 101}},
            ),
        ]
    )
    monkeypatch.setattr(sources.requests, "get", get)
    sleep = Mock()
    monkeypatch.setattr(sources.time, "sleep", sleep)

    dependents = sources.fetch_crates_io_reverse_deps("pkarr")

    assert len(dependents) == 101
    assert dependents[0]["crate"] == "first"
    assert dependents[-1]["crate"] == "last"
    assert dependents[0]["repository"] == "https://github.com/example/first"
    assert [call.kwargs["params"]["page"] for call in get.call_args_list] == [1, 2]
    assert all(call.kwargs["timeout"] == 30 for call in get.call_args_list)
    sleep.assert_called_once_with(sources.CRATES_IO_DELAY)


@pytest.mark.parametrize("status", [404, 500])
def test_reverse_dependency_errors_are_not_reported_as_success(monkeypatch, status):
    monkeypatch.setattr(sources.requests, "get", Mock(return_value=_response(status)))

    with pytest.raises(requests.HTTPError):
        sources.fetch_crates_io_reverse_deps("pkarr")


@pytest.mark.parametrize("failure", [requests.Timeout("offline"), _response(503)])
def test_optional_download_failures_return_none(monkeypatch, failure):
    get = Mock(side_effect=[failure, failure])
    monkeypatch.setattr(sources.requests, "get", get)

    assert sources.fetch_crates_io_downloads("pkarr") is None
    assert sources.fetch_npm_downloads("@example/pkarr") is None
    assert get.call_count == 2


def test_dependents_page_retries_stop_after_exhaustion(monkeypatch):
    get = Mock(side_effect=requests.Timeout("offline"))
    sleep = Mock()
    monkeypatch.setattr(sources.requests, "get", get)
    monkeypatch.setattr(sources.time, "sleep", sleep)

    with pytest.raises(sources.SourceError):
        sources.scrape_github_dependents("pubky/pkarr")

    assert get.call_count == sources.SCRAPE_MAX_RETRIES
    assert [call.args[0] for call in sleep.call_args_list] == [2, 4]


def _search_response(items, total=None, incomplete=False):
    return subprocess.CompletedProcess(
        [],
        0,
        json.dumps(
            {
                "total_count": len(items) if total is None else total,
                "incomplete_results": incomplete,
                "items": items,
            }
        ),
        "",
    )


def _items(count, offset=0):
    return [
        {"repository": {"full_name": f"example/repo{i}"}, "path": "Cargo.lock"}
        for i in range(offset, offset + count)
    ]


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CompletedProcess([], 1, "", "HTTP 429: rate limit"),
        subprocess.TimeoutExpired(["gh"], 60),
    ],
)
def test_gh_retries_are_finite(monkeypatch, failure):
    run = Mock(side_effect=[failure] * sources.GH_MAX_ATTEMPTS)
    sleep = Mock()
    monkeypatch.setattr(sources.subprocess, "run", run)
    monkeypatch.setattr(sources.time, "sleep", sleep)
    with pytest.raises(sources.SourceError, match="after 4 attempts"):
        sources.search_github_cargo_lock("pkarr")
    assert run.call_count == sources.GH_MAX_ATTEMPTS
    assert sleep.call_count == sources.GH_MAX_ATTEMPTS


@pytest.mark.parametrize(
    "response",
    [
        _search_response([], total=1001),
        _search_response([], total=1000),
        _search_response([], total=True),
        _search_response([], total=-1),
        _search_response([], incomplete="true"),
        _search_response(
            [{"repository": {"full_name": "//evil"}, "path": "Cargo.lock"}]
        ),
        _search_response([{"repository": {}, "path": "Cargo.lock"}]),
        _search_response([{"repository": {"full_name": "example/repo"}}]),
        subprocess.CompletedProcess([], 0, "not JSON", ""),
    ],
)
def test_search_rejects_malformed_and_capped_results_without_retry(
    monkeypatch, response
):
    run = Mock(return_value=response)
    monkeypatch.setattr(sources, "_run_gh", run)
    with pytest.raises(sources.SourceError):
        sources.search_github_cargo_lock("pkarr")
    run.assert_called_once()


@pytest.mark.parametrize(
    "response",
    [
        _search_response([], total=8),
        _search_response([], incomplete=True),
        _search_response(_items(1) * 2),
    ],
)
def test_search_rejects_results_that_never_stabilize_after_three_attempts(
    monkeypatch, response
):
    run = Mock(return_value=response)
    sleep = Mock()
    monkeypatch.setattr(sources, "_run_gh", run)
    monkeypatch.setattr(sources.time, "sleep", sleep)

    with pytest.raises(sources.SourceError):
        sources.search_github_cargo_lock("pkarr")

    assert run.call_count == 3
    assert all("page=1" in call.args[0] for call in run.call_args_list)
    assert [call.args[0] for call in sleep.call_args_list] == [15, 30]


@pytest.mark.parametrize(
    "response",
    [
        _search_response([], total=8),
        _search_response(_items(1), incomplete=True),
        _search_response(_items(1) * 2),
    ],
)
def test_search_retries_transient_first_page_and_uses_only_stable_results(
    monkeypatch, response
):
    run = Mock(side_effect=[response, _search_response(_items(2, 200))])
    monkeypatch.setattr(sources, "_run_gh", run)

    matches = sources.search_github_cargo_lock("pkarr")

    assert [match.repo for match in matches] == ["example/repo200", "example/repo201"]
    assert all("page=1" in call.args[0] for call in run.call_args_list)


def test_search_verifies_every_page(monkeypatch):
    run = Mock(
        side_effect=[
            _search_response(_items(100), 101),
            _search_response(_items(1, 100), 101),
        ]
    )
    monkeypatch.setattr(sources, "_run_gh", run)
    assert len(sources.search_github_cargo_lock("pkarr")) == 101
    assert "page=2" in run.call_args.args[0]
    assert "github.com" in run.call_args.args[0]


@pytest.mark.parametrize(
    "last",
    [
        _search_response([], 101),
        _search_response(_items(1), 101),
        _search_response(_items(1, 100), 102),
    ],
)
def test_later_search_page_failure_discards_earlier_results(monkeypatch, last):
    run = Mock(side_effect=[_search_response(_items(100), 101), last] * 3)
    monkeypatch.setattr(sources, "_run_gh", run)
    with pytest.raises(sources.SourceError):
        sources.search_github_cargo_lock("pkarr")
    assert run.call_count == 6
    assert [
        next(arg for arg in call.args[0] if arg.startswith("page="))
        for call in run.call_args_list
    ] == ["page=1", "page=2"] * 3


@pytest.mark.parametrize(
    "last",
    [
        _search_response([], 101),
        _search_response(_items(1), 101),
        _search_response(_items(1, 100), 102),
        _search_response(_items(1, 100), 101, incomplete=True),
    ],
)
def test_later_search_page_instability_restarts_from_page_one(monkeypatch, last):
    run = Mock(
        side_effect=[
            _search_response(_items(100), 101),
            last,
            _search_response(_items(100, 200), 101),
            _search_response(_items(1, 300), 101),
        ]
    )
    monkeypatch.setattr(sources, "_run_gh", run)

    matches = sources.search_github_cargo_lock("pkarr")

    assert {match.repo for match in matches} == {
        f"example/repo{i}" for i in range(200, 301)
    }
    assert [
        next(arg for arg in call.args[0] if arg.startswith("page="))
        for call in run.call_args_list
    ] == ["page=1", "page=2", "page=1", "page=2"]


def test_search_transport_failure_does_not_multiply_http_retry_budget(monkeypatch):
    run = Mock(
        side_effect=sources.SourceError("GitHub request failed after 4 attempts")
    )
    monkeypatch.setattr(sources, "_run_gh", run)

    with pytest.raises(sources.SourceError, match="after 4 attempts"):
        sources.search_github_cargo_lock("pkarr")

    run.assert_called_once()


def test_successful_empty_search_is_allowed(monkeypatch):
    monkeypatch.setattr(sources, "_run_gh", Mock(return_value=_search_response([])))
    assert sources.search_github_cargo_lock("pkarr") == []


def _dependents_html(next_url=None):
    html = '<meta name="route-action" content="dependents"><a data-hovercard-type="repository" href="/example/repo">repo</a>'
    if next_url:
        html += f'<a class="btn" href="{next_url}">Next</a>'
    return html


@pytest.mark.parametrize(
    "second",
    [
        sources.SourceError("offline"),
        '<meta content="dependents">Repository dependents are currently unavailable.',
    ],
)
def test_failed_later_dependents_page_rejects_partial_list(monkeypatch, second):
    fetch = Mock(side_effect=[_dependents_html("?page=2"), second])
    monkeypatch.setattr(sources, "_fetch_dependents_page", fetch)
    monkeypatch.setattr(sources.time, "sleep", Mock())
    with pytest.raises(sources.SourceError):
        sources.scrape_github_dependents("pubky/pkarr")


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/steal",
        "//evil.example/steal",
        "/pubky/other/network/dependents",
        "https://github.com/pubky/pkarr/network/dependents",
    ],
)
def test_dependents_pagination_rejects_external_and_looping_urls(monkeypatch, url):
    fetch = Mock(return_value=_dependents_html(url))
    monkeypatch.setattr(sources, "_fetch_dependents_page", fetch)
    monkeypatch.setattr(sources.time, "sleep", Mock())
    with pytest.raises(sources.SourceError):
        sources.scrape_github_dependents("pubky/pkarr")
    fetch.assert_called_once()


def test_classification_transport_failure_is_not_a_missing_file(monkeypatch):
    monkeypatch.setattr(
        sources, "_run_gh", Mock(side_effect=sources.SourceError("offline"))
    )
    with pytest.raises(sources.SourceError):
        sources.fetch_file_content("example/repo", "Cargo.toml")
    assert sources.fetch_github_stars("example/repo") is None


def test_npm_preserves_configured_sample_size_but_checks_completeness(monkeypatch):
    monkeypatch.setattr(
        sources.requests,
        "get",
        Mock(
            return_value=_response(
                200,
                {
                    "total": 2,
                    "objects": [
                        {"package": {"name": "target"}},
                        {"package": {"name": "client"}},
                    ],
                },
            )
        ),
    )
    monkeypatch.setattr(
        sources, "_run_gh", Mock(return_value=_search_response(_items(50), 500))
    )
    results = sources.search_npm_dependents("target")
    assert len(results) == 51
    assert results[0] == {
        "package": "client",
        "description": "",
        "source": "npm_registry",
    }
    monkeypatch.setattr(
        sources,
        "_run_gh",
        Mock(return_value=_search_response(_items(50), 500, incomplete=True)),
    )
    with pytest.raises(sources.SourceError):
        sources.search_npm_dependents("target")


@pytest.mark.parametrize(
    "response", [_response(503), _response(200, {"total": 8, "objects": []})]
)
def test_npm_registry_failure_is_not_partial_success(monkeypatch, response):
    monkeypatch.setattr(sources.requests, "get", Mock(return_value=response))
    with pytest.raises(sources.SourceError):
        sources.search_npm_dependents("target")
