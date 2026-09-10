"""Publication is all-or-nothing and recovery stops after a complete cycle."""

import json
from datetime import datetime
from pathlib import Path

import pytest

import analyze
from publication import (
    CORE_SOURCES,
    recovery_due,
    validate_candidate,
    validate_snapshot,
)
from sources import SourceError


def snapshot(crate="pkarr", count=100, run_id="2026-08-31T06:00:00+00:00"):
    return {
        "crate": crate,
        "updated_at": run_id,
        "total": count,
        "summary": {"direct": count},
        "lists": {"direct": [{"repo": f"example/repo{i}"} for i in range(count)]},
        "collection": {
            "status": "complete",
            "run_id": run_id,
            "sources": dict.fromkeys(CORE_SOURCES, count),
        },
    }


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "partial"),
        ("sources", {}),
        ("run_id", "2026-08-31T06:00:00"),
        ("run_id", "2099-01-01T00:00:00Z"),
    ],
)
def test_metadata_must_describe_a_complete_past_measurement(field, value):
    data = snapshot()
    data["collection"][field] = value
    with pytest.raises((ValueError, KeyError)):
        validate_snapshot(data, {"crate": "pkarr"})


def test_candidate_rejects_large_decline_without_rewriting_previous(tmp_path):
    previous = tmp_path / "docs" / "pkarr.json"
    candidate = tmp_path / "staging" / "pkarr.json"
    write(previous, snapshot())
    write(candidate, snapshot(count=10))
    original = previous.read_bytes()
    with pytest.raises(ValueError, match="Suspicious decrease"):
        validate_candidate(candidate, previous.parent, {"crate": "pkarr"})
    assert previous.read_bytes() == original
    validate_candidate(
        candidate, previous.parent, {"crate": "pkarr"}, allow_decrease=True
    )


def test_override_never_bypasses_completeness_validation(tmp_path):
    candidate = tmp_path / "pkarr.json"
    data = snapshot()
    data["collection"]["status"] = "partial"
    write(candidate, data)
    with pytest.raises(ValueError, match="completed collections"):
        validate_candidate(candidate, tmp_path, {"crate": "pkarr"}, allow_decrease=True)


def test_source_drop_rejected_even_when_final_count_is_stable(tmp_path):
    previous = tmp_path / "docs" / "pkarr.json"
    candidate = tmp_path / "staging" / "pkarr.json"
    write(previous, snapshot())
    data = snapshot()
    data["collection"]["sources"]["github_cargo_lock"] = 10
    write(candidate, data)
    with pytest.raises(ValueError, match="github_cargo_lock"):
        validate_candidate(candidate, previous.parent, {"crate": "pkarr"})


def test_legacy_publication_still_protects_against_total_drop(tmp_path):
    previous = snapshot()
    del previous["collection"]
    write(tmp_path / "docs" / "pkarr.json", previous)
    candidate = tmp_path / "staging" / "pkarr.json"
    write(candidate, snapshot(count=0))
    with pytest.raises(ValueError, match="Suspicious decrease"):
        validate_candidate(candidate, tmp_path / "docs", {"crate": "pkarr"})


@pytest.mark.parametrize("failure", ["second_source", "second_validation"])
def test_entire_batch_preserved_when_one_crate_fails(monkeypatch, tmp_path, failure):
    monkeypatch.chdir(tmp_path)
    configs = [
        {"crate": crate, "github_repo": f"pubky/{crate}"}
        for crate in ("pkarr", "mainline")
    ]
    write(tmp_path / "crates.json", configs)
    for config in configs:
        write(tmp_path / "docs" / f"{config['crate']}.json", snapshot(config["crate"]))
    original = {path.name: path.read_bytes() for path in Path("docs").glob("*.json")}

    def fake_analysis(crate, repo, *, output_dir, run_id, **kwargs):
        if crate == "mainline" and failure == "second_source":
            raise SourceError("second crate failed")
        count = 0 if crate == "mainline" else 100
        path = Path(output_dir) / f"{crate}.json"
        write(path, snapshot(crate, count, run_id))
        return str(path)

    monkeypatch.setattr(analyze, "analyze_crate", fake_analysis)
    with pytest.raises((SourceError, ValueError)):
        analyze.main([])
    assert {
        path.name: path.read_bytes() for path in Path("docs").glob("*.json")
    } == original


def test_successful_batch_has_common_run_identity(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    configs = [
        {"crate": crate, "github_repo": f"pubky/{crate}"}
        for crate in ("pkarr", "mainline")
    ]
    write(tmp_path / "crates.json", configs)

    def fake_analysis(crate, repo, *, output_dir, run_id, **kwargs):
        path = Path(output_dir) / f"{crate}.json"
        write(path, snapshot(crate, run_id=run_id))
        return str(path)

    monkeypatch.setattr(analyze, "analyze_crate", fake_analysis)
    analyze.main([])
    outputs = [json.loads(path.read_text()) for path in Path("docs").glob("*.json")]
    assert {data["crate"] for data in outputs} == {"pkarr", "mainline"}
    assert len({data["collection"]["run_id"] for data in outputs}) == 1


@pytest.mark.parametrize(
    "now", ["2026-08-31T12:00:00Z", "2026-08-31T18:00:00Z", "2026-09-01T00:00:00Z"]
)
def test_recovery_skips_analysis_after_this_cycle_succeeds(tmp_path, now):
    write(tmp_path / "pkarr.json", snapshot())
    assert not recovery_due(tmp_path, [{"crate": "pkarr"}], datetime.fromisoformat(now))


@pytest.mark.parametrize(
    "condition", ["missing", "legacy", "old", "mixed_run", "invalid"]
)
def test_recovery_retries_until_all_crates_are_complete(tmp_path, condition):
    configs = [{"crate": "pkarr"}, {"crate": "mainline"}]
    write(tmp_path / "pkarr.json", snapshot())
    data = snapshot("mainline")
    if condition == "legacy":
        del data["collection"]
    elif condition == "old":
        data = snapshot("mainline", run_id="2026-08-24T06:00:00Z")
    elif condition == "mixed_run":
        data = snapshot("mainline", run_id="2026-08-31T07:00:00Z")
    elif condition == "invalid":
        data["total"] = 3
    if condition != "missing":
        write(tmp_path / "mainline.json", data)
    assert recovery_due(
        tmp_path, configs, datetime.fromisoformat("2026-08-31T12:00:00Z")
    )


def test_next_week_starts_a_new_cycle(tmp_path):
    write(tmp_path / "pkarr.json", snapshot())
    assert recovery_due(
        tmp_path, [{"crate": "pkarr"}], datetime.fromisoformat("2026-09-07T06:00:00Z")
    )
