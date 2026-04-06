"""Tests for list categorization logic.

Given classification results for repos, assign each to the correct list.
Lists are named after direct dependants of the target crate.
"""

from classify import categorize


def test_direct_dep_goes_to_direct_list():
    repos = [
        {
            "repo": "n0-computer/iroh",
            "classification": {"kind": "direct"},
            "chain": ["iroh", "pkarr"],
        }
    ]
    result = categorize(repos, "pkarr")
    assert "n0-computer/iroh" in [r["repo"] for r in result["direct"]]


def test_transitive_via_iroh_goes_to_iroh_list():
    repos = [
        {
            "repo": "n0-computer/iroh",
            "classification": {"kind": "direct"},
            "chain": ["iroh", "pkarr"],
        },
        {
            "repo": "moq-dev/moq",
            "classification": None,
            "chain": ["moq-native", "web-transport-iroh", "iroh", "pkarr"],
        },
    ]
    result = categorize(repos, "pkarr")
    assert "moq-dev/moq" in [r["repo"] for r in result["iroh"]]


def test_transitive_via_fedimint_goes_to_fedimint_list():
    """If a project depends on fedimint-server which depends on pkarr,
    it should go in the fedimint-server list."""
    repos = [
        {
            "repo": "fedimint/fedimint",
            "classification": {"kind": "direct"},
            "chain": ["fedimint-server", "pkarr"],
        },
        {
            "repo": "some/fedimint-plugin",
            "classification": None,
            "chain": ["plugin", "fedimint-server", "pkarr"],
        },
    ]
    result = categorize(repos, "pkarr")
    assert "some/fedimint-plugin" in [
        r["repo"] for r in result["fedimint-server"]
    ]


def test_deep_chain_categorized_by_direct_parent_of_target():
    """holochain → kitsune2 → iroh → pkarr should be in the iroh list,
    because iroh is the direct dependant of pkarr."""
    repos = [
        {
            "repo": "n0-computer/iroh",
            "classification": {"kind": "direct"},
            "chain": ["iroh", "pkarr"],
        },
        {
            "repo": "holochain/holochain",
            "classification": None,
            "chain": [
                "holochain_conductor_api",
                "kitsune2_transport_iroh",
                "iroh",
                "pkarr",
            ],
        },
    ]
    result = categorize(repos, "pkarr")
    assert "holochain/holochain" in [r["repo"] for r in result["iroh"]]


def test_feature_flag_categorized_under_parent_crate():
    """worldcoin uses iroh with discovery-pkarr-dht feature.
    Should go in the iroh list."""
    repos = [
        {
            "repo": "n0-computer/iroh",
            "classification": {"kind": "direct"},
            "chain": ["iroh", "pkarr"],
        },
        {
            "repo": "worldcoin/orb-software",
            "classification": {"kind": "feature_flag", "parent_crate": "iroh"},
            "chain": ["orb-blob", "iroh", "pkarr"],
        },
    ]
    result = categorize(repos, "pkarr")
    assert "worldcoin/orb-software" in [r["repo"] for r in result["iroh"]]


def test_direct_deps_also_create_list_names():
    """Each direct dependant should appear in the 'direct' list AND
    create a potential list name for transitive deps."""
    repos = [
        {
            "repo": "n0-computer/iroh",
            "classification": {"kind": "direct"},
            "chain": ["iroh", "pkarr"],
        },
        {
            "repo": "fedimint/fedimint",
            "classification": {"kind": "direct"},
            "chain": ["fedimint-server", "pkarr"],
        },
    ]
    result = categorize(repos, "pkarr")
    direct_repos = [r["repo"] for r in result["direct"]]
    assert "n0-computer/iroh" in direct_repos
    assert "fedimint/fedimint" in direct_repos


def test_chain_preserved_in_output():
    repos = [
        {
            "repo": "n0-computer/iroh",
            "classification": {"kind": "direct"},
            "chain": ["iroh", "pkarr"],
        },
        {
            "repo": "cablehead/http-nu",
            "classification": None,
            "chain": ["http-nu", "cross-stream", "iroh", "pkarr"],
        },
    ]
    result = categorize(repos, "pkarr")
    http_nu = [r for r in result["iroh"] if r["repo"] == "cablehead/http-nu"][0]
    assert http_nu["chain"] == ["http-nu", "cross-stream", "iroh", "pkarr"]
