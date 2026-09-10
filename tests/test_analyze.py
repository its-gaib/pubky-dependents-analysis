"""Offline tests for gathering, classifying, and writing analysis results."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

import analyze
from sources import RepoMatch

FIXTURES = Path(__file__).parent / "fixtures"


def test_gather_merges_sources_preserves_paths_and_excludes_target(monkeypatch):
    sources = {
        "fetch_crates_io_reverse_deps": [
            {"repository": "https://github.com/example/shared.git"},
            {"repository": "https://github.com/example/published/"},
            {"repository": "https://gitlab.com/example/elsewhere"},
            {"repository": None},
        ],
        "search_github_cargo_toml": [
            RepoMatch("example/shared", cargo_toml_paths=["sdk/Cargo.toml"]),
            RepoMatch("pubky/pkarr", cargo_toml_paths=["Cargo.toml"]),
        ],
        "search_github_cargo_lock": [
            RepoMatch("example/shared", cargo_lock_paths=["workspace/Cargo.lock"]),
            RepoMatch("example/locked", cargo_lock_paths=["Cargo.lock"]),
        ],
        "scrape_github_dependents": [
            "example/shared",
            "example/scraped",
            "pubky/pkarr",
        ],
    }
    for name, results in sources.items():
        monkeypatch.setattr(analyze, name, Mock(return_value=results))

    repos = analyze._gather_repos("pkarr", "pubky/pkarr")

    assert set(repos) == {
        "example/shared",
        "example/published",
        "example/locked",
        "example/scraped",
    }
    assert repos["example/shared"].cargo_toml_paths == ["sdk/Cargo.toml"]
    assert repos["example/shared"].cargo_lock_paths == ["workspace/Cargo.lock"]


@pytest.mark.parametrize("include_downloads", [False, True])
def test_pipeline_writes_categorized_json_offline(
    monkeypatch, tmp_path, include_downloads
):
    monkeypatch.chdir(tmp_path)
    repos = {
        "n0-computer/iroh": RepoMatch("n0-computer/iroh"),
        "cablehead/http-nu": RepoMatch("cablehead/http-nu"),
        "example/unrelated": RepoMatch("example/unrelated"),
    }
    contents = {
        ("n0-computer/iroh", "Cargo.toml"): (
            FIXTURES / "iroh_cargo_toml.toml"
        ).read_text(),
        ("cablehead/http-nu", "Cargo.lock"): (
            FIXTURES / "http_nu_cargo_lock.toml"
        ).read_text(),
    }
    monkeypatch.setattr(analyze, "_gather_repos", Mock(return_value=repos))
    monkeypatch.setattr(analyze, "fetch_file_content", lambda *key: contents.get(key))
    monkeypatch.setattr(analyze.time, "sleep", Mock())
    stars = Mock(
        side_effect=lambda repo: {"n0-computer/iroh": 100, "cablehead/http-nu": 5}[repo]
    )
    monkeypatch.setattr(analyze, "fetch_github_stars", stars)
    downloads = {"total": 0, "recent": 0} if include_downloads else None
    npm_dependents = [{"package": "example/client", "source": "npm_registry"}]
    npm_search = Mock(return_value=npm_dependents)
    npm_fetch = Mock(return_value=downloads)
    monkeypatch.setattr(
        analyze, "fetch_crates_io_downloads", Mock(return_value=downloads)
    )
    monkeypatch.setattr(analyze, "fetch_npm_downloads", npm_fetch)
    monkeypatch.setattr(analyze, "search_npm_dependents", npm_search)

    output_path = analyze.analyze_crate(
        "pkarr",
        "pubky/pkarr",
        npm_package="@example/pkarr" if include_downloads else None,
        react_native_package="@example/pkarr-native" if include_downloads else None,
    )

    assert Path(output_path) == Path("docs/pkarr.json")
    output = json.loads(Path(output_path).read_text())
    assert output["crate"] == "pkarr"
    assert datetime.fromisoformat(output["updated_at"]).utcoffset() == timedelta(0)
    assert output["total"] == 2
    assert output["summary"] == {"direct": 1, "iroh": 1}
    assert output["lists"]["direct"] == [
        {
            "repo": "n0-computer/iroh",
            "chain": ["iroh", "pkarr"],
            "stars": 100,
            "version": "5",
            "default_features": False,
        }
    ]
    indirect = output["lists"]["iroh"][0]
    assert indirect["repo"] == "cablehead/http-nu"
    assert indirect["chain"] == ["http-nu", "cross-stream", "iroh", "pkarr"]
    assert indirect["stars"] == 5
    assert stars.call_count == 2
    optional_keys = {
        "crates_io_downloads",
        "npm_downloads",
        "rn_downloads",
        "npm_dependents",
    }
    if include_downloads:
        assert output["npm_dependents"] == npm_dependents
        for key in optional_keys - {"npm_dependents"}:
            assert output[key] == {"total": 0, "recent": 0}
        npm_search.assert_called_once_with("@example/pkarr")
        assert [call.args[0] for call in npm_fetch.call_args_list] == [
            "@example/pkarr",
            "@example/pkarr-native",
        ]
    else:
        assert not optional_keys.intersection(output)
        npm_search.assert_not_called()
        npm_fetch.assert_not_called()


def test_classify_repo_skips_malformed_lockfile_and_uses_next_match(monkeypatch):
    contents = {
        "broken/Cargo.lock": '# pkarr\n[[package]\nname = "broken"',
        "Cargo.lock": (FIXTURES / "http_nu_cargo_lock.toml").read_text(),
    }
    monkeypatch.setattr(
        analyze, "fetch_file_content", lambda repo, path: contents.get(path)
    )
    match = RepoMatch(
        "cablehead/http-nu", cargo_lock_paths=["broken/Cargo.lock", "Cargo.lock"]
    )

    result = analyze._classify_repo(match.repo, match, "pkarr")

    assert result is not None
    assert result.repo == "cablehead/http-nu"
    assert result.chain == ["http-nu", "cross-stream", "iroh", "pkarr"]
