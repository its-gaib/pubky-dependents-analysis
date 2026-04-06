"""Tests for list categorization logic."""

from classify import (
    CategorizedEntry,
    Classification,
    RepoAnalysis,
    categorize,
)


def test_direct_dep_goes_to_direct_list():
    repos = [
        RepoAnalysis(
            repo="n0-computer/iroh",
            classification=Classification(kind="direct"),
            chain=["iroh", "pkarr"],
        )
    ]
    result = categorize(repos, "pkarr")
    assert "n0-computer/iroh" in [r.repo for r in result["direct"]]


def test_transitive_via_iroh_goes_to_iroh_list():
    repos = [
        RepoAnalysis(
            repo="n0-computer/iroh",
            classification=Classification(kind="direct"),
            chain=["iroh", "pkarr"],
        ),
        RepoAnalysis(
            repo="moq-dev/moq",
            classification=None,
            chain=["moq-native", "web-transport-iroh", "iroh", "pkarr"],
        ),
    ]
    result = categorize(repos, "pkarr")
    assert "moq-dev/moq" in [r.repo for r in result["iroh"]]


def test_transitive_via_fedimint_goes_to_fedimint_list():
    repos = [
        RepoAnalysis(
            repo="fedimint/fedimint",
            classification=Classification(kind="direct"),
            chain=["fedimint-server", "pkarr"],
        ),
        RepoAnalysis(
            repo="some/fedimint-plugin",
            classification=None,
            chain=["plugin", "fedimint-server", "pkarr"],
        ),
    ]
    result = categorize(repos, "pkarr")
    assert "some/fedimint-plugin" in [
        r.repo for r in result["fedimint-server"]
    ]


def test_deep_chain_categorized_by_direct_parent_of_target():
    repos = [
        RepoAnalysis(
            repo="n0-computer/iroh",
            classification=Classification(kind="direct"),
            chain=["iroh", "pkarr"],
        ),
        RepoAnalysis(
            repo="holochain/holochain",
            classification=None,
            chain=[
                "holochain_conductor_api",
                "kitsune2_transport_iroh",
                "iroh",
                "pkarr",
            ],
        ),
    ]
    result = categorize(repos, "pkarr")
    assert "holochain/holochain" in [r.repo for r in result["iroh"]]


def test_feature_flag_categorized_under_parent_crate():
    repos = [
        RepoAnalysis(
            repo="n0-computer/iroh",
            classification=Classification(kind="direct"),
            chain=["iroh", "pkarr"],
        ),
        RepoAnalysis(
            repo="worldcoin/orb-software",
            classification=Classification(kind="feature_flag", parent_crate="iroh"),
            chain=["orb-blob", "iroh", "pkarr"],
        ),
    ]
    result = categorize(repos, "pkarr")
    assert "worldcoin/orb-software" in [r.repo for r in result["iroh"]]


def test_direct_deps_also_create_list_names():
    repos = [
        RepoAnalysis(
            repo="n0-computer/iroh",
            classification=Classification(kind="direct"),
            chain=["iroh", "pkarr"],
        ),
        RepoAnalysis(
            repo="fedimint/fedimint",
            classification=Classification(kind="direct"),
            chain=["fedimint-server", "pkarr"],
        ),
    ]
    result = categorize(repos, "pkarr")
    direct_repos = [r.repo for r in result["direct"]]
    assert "n0-computer/iroh" in direct_repos
    assert "fedimint/fedimint" in direct_repos


def test_chain_preserved_in_output():
    repos = [
        RepoAnalysis(
            repo="n0-computer/iroh",
            classification=Classification(kind="direct"),
            chain=["iroh", "pkarr"],
        ),
        RepoAnalysis(
            repo="cablehead/http-nu",
            classification=None,
            chain=["http-nu", "cross-stream", "iroh", "pkarr"],
        ),
    ]
    result = categorize(repos, "pkarr")
    http_nu = [r for r in result["iroh"] if r.repo == "cablehead/http-nu"][0]
    assert http_nu.chain == ["http-nu", "cross-stream", "iroh", "pkarr"]
