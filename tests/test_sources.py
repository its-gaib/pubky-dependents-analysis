"""Source adapter behavior with no HTTP requests, subprocesses, or real waits."""

import json
import subprocess
from unittest.mock import Mock

import pytest
import requests

import sources


def _response(status, payload=None):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode()
    return response


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

    assert sources._run_gh(command, timeout=15, pre_delay=5) is success

    assert [call.args[0] for call in sleep.call_args_list] == [
        5,
        30,
        60,
        120,
        240,
        480,
        600,
        600,
    ]
    assert run.call_count == 8
    run.assert_called_with(
        command, capture_output=True, text=True, timeout=15, check=False
    )


@pytest.mark.parametrize("failure", ["not_found", "forbidden", "invalid_json"])
def test_gh_search_errors_return_no_results_without_retrying(monkeypatch, failure):
    results = {
        "not_found": FileNotFoundError("gh is not installed"),
        "forbidden": subprocess.CompletedProcess([], 1, "", "HTTP 403: Forbidden"),
        "invalid_json": subprocess.CompletedProcess([], 0, "not JSON", ""),
    }
    run = Mock(side_effect=[results[failure]])
    sleep = Mock()
    monkeypatch.setattr(sources.subprocess, "run", run)
    monkeypatch.setattr(sources.time, "sleep", sleep)

    assert sources.search_github_cargo_toml("pkarr") == []

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
        {"repository": {"nameWithOwner": repo}, "path": path}
        for repo, path in [
            ("example/shared", filename),
            ("example/shared", f"workspace/{filename}"),
            ("example/other", filename),
        ]
    ]
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(items), ""))
    monkeypatch.setattr(sources.subprocess, "run", run)
    monkeypatch.setattr(sources.time, "sleep", Mock())

    matches = {match.repo: match for match in search("pkarr")}

    assert set(matches) == {"example/shared", "example/other"}
    assert getattr(matches["example/shared"], path_attribute) == [
        filename,
        f"workspace/{filename}",
    ]
    assert getattr(matches["example/other"], path_attribute) == [filename]
    assert filename in run.call_args.args[0]


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
                        }
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

    assert [dep["crate"] for dep in dependents] == ["first", "last"]
    assert dependents[0]["repository"] == "https://github.com/example/first"
    assert [call.kwargs["params"]["page"] for call in get.call_args_list] == [1, 2]
    assert all(call.kwargs["timeout"] == 30 for call in get.call_args_list)
    sleep.assert_called_once_with(sources.CRATES_IO_DELAY)


@pytest.mark.parametrize("status", [404, 500])
def test_reverse_dependency_errors_are_not_reported_as_success(monkeypatch, status):
    monkeypatch.setattr(sources.requests, "get", Mock(return_value=_response(status)))

    if status == 404:
        assert sources.fetch_crates_io_reverse_deps("missing") == []
    else:
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

    assert sources.scrape_github_dependents("pubky/pkarr") == []

    assert get.call_count == sources.SCRAPE_MAX_RETRIES
    assert [call.args[0] for call in sleep.call_args_list] == [2, 4]
