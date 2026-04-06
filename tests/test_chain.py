"""Tests for Cargo.lock dependency chain tracing.

Given a Cargo.lock string, find the shortest dependency chain(s)
from root crate(s) to a target crate.
"""

from pathlib import Path

from classify import trace_chains

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_moq_chain_to_pkarr():
    """moq-cli → moq-native → web-transport-iroh → iroh → pkarr"""
    chains = trace_chains(_read("moq_cargo_lock.toml"), "pkarr")
    # Find the chain that starts from a moq crate
    moq_chains = [c for c in chains if c[0].startswith("moq")]
    assert len(moq_chains) >= 1
    chain = moq_chains[0]
    assert chain[-1] == "pkarr"
    assert "iroh" in chain  # iroh must be in the path


def test_holochain_chain_to_pkarr():
    """Some holochain crate → ... → kitsune2_transport_iroh → iroh → pkarr"""
    chains = trace_chains(_read("holochain_cargo_lock.toml"), "pkarr")
    # iroh must be the direct parent of pkarr in all chains
    for chain in chains:
        pkarr_idx = chain.index("pkarr")
        assert chain[pkarr_idx - 1] in ("iroh", "iroh-relay", "iroh-relay-holochain")


def test_http_nu_chain_to_pkarr():
    """http-nu → cross-stream → iroh → pkarr"""
    chains = trace_chains(_read("http_nu_cargo_lock.toml"), "pkarr")
    http_chains = [c for c in chains if c[0] == "http-nu"]
    assert len(http_chains) >= 1
    chain = http_chains[0]
    assert "cross-stream" in chain
    assert "iroh" in chain
    assert chain[-1] == "pkarr"


def test_spacedrive_chain_to_pkarr():
    """Some spacedrive crate → sd-core → iroh → pkarr"""
    chains = trace_chains(_read("spacedrive_cargo_lock.toml"), "pkarr")
    # Find chain going through sd-core
    sd_chains = [c for c in chains if "sd-core" in c]
    assert len(sd_chains) >= 1
    chain = sd_chains[0]
    assert "iroh" in chain
    assert chain[-1] == "pkarr"


def test_worldcoin_chain_to_pkarr():
    """orb-* → iroh → pkarr"""
    chains = trace_chains(_read("worldcoin_cargo_lock.toml"), "pkarr")
    assert len(chains) >= 1
    for chain in chains:
        assert chain[-1] == "pkarr"
        pkarr_idx = chain.index("pkarr")
        assert chain[pkarr_idx - 1] in ("iroh", "iroh-relay")


def test_chain_identifies_direct_pkarr_parent():
    """In all transitive cases, the crate immediately before pkarr
    in the chain should be a known direct dependant."""
    for fixture in [
        "moq_cargo_lock.toml",
        "holochain_cargo_lock.toml",
        "http_nu_cargo_lock.toml",
        "spacedrive_cargo_lock.toml",
        "worldcoin_cargo_lock.toml",
    ]:
        chains = trace_chains(_read(fixture), "pkarr")
        for chain in chains:
            pkarr_idx = chain.index("pkarr")
            parent = chain[pkarr_idx - 1]
            # The direct parent of pkarr should be an iroh-ecosystem crate
            assert parent.startswith("iroh"), (
                f"In {fixture}, unexpected direct pkarr parent: {parent}"
            )
