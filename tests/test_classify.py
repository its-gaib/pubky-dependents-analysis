"""Tests for Cargo.toml classification logic."""

from pathlib import Path

from classify import classify_cargo_toml

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


# --- Direct dependency cases ---


def test_iroh_has_direct_pkarr_dep():
    result = classify_cargo_toml(_read("iroh_cargo_toml.toml"), "pkarr")
    assert result.kind == "direct"
    assert result.version == "5"
    assert result.default_features is False


def test_fedimint_server_has_direct_pkarr_dep():
    result = classify_cargo_toml(
        _read("fedimint_server_cargo_toml.toml"), "pkarr"
    )
    assert result.kind == "direct"
    assert "dht" in result.features
    assert "relays" in result.features


def test_fedimint_workspace_has_direct_pkarr_dep():
    result = classify_cargo_toml(
        _read("fedimint_workspace_cargo_toml.toml"), "pkarr"
    )
    assert result.kind == "direct"
    assert result.version == "3.10.0"


def test_pubky_sdk_has_direct_pkarr_dep():
    result = classify_cargo_toml(_read("pubky_cargo_toml.toml"), "pkarr")
    assert result.kind == "direct"
    assert "full" in result.features


def test_pubky_workspace_has_direct_pkarr_dep():
    result = classify_cargo_toml(
        _read("pubky_workspace_cargo_toml.toml"), "pkarr"
    )
    assert result.kind == "direct"
    assert result.version == "5.0.3"


def test_jetstream_iroh_has_direct_pkarr_dep():
    result = classify_cargo_toml(
        _read("jetstream_iroh_cargo_toml.toml"), "pkarr"
    )
    assert result.kind == "direct"
    assert result.version == "5.0"


def test_okid_has_direct_optional_pkarr_dep():
    result = classify_cargo_toml(_read("okid_cargo_toml.toml"), "pkarr")
    assert result.kind == "direct"
    assert result.optional is True
    assert result.version == "5.0.0"


# --- Feature flag only cases ---


def test_worldcoin_has_pkarr_only_in_iroh_features():
    result = classify_cargo_toml(
        _read("worldcoin_orb_blob_cargo_toml.toml"), "pkarr"
    )
    assert result.kind == "feature_flag"
    assert result.parent_crate == "iroh"


# --- Not present cases ---


def test_moq_cargo_toml_has_no_pkarr():
    result = classify_cargo_toml(_read("moq_cargo_toml.toml"), "pkarr")
    assert result is None
