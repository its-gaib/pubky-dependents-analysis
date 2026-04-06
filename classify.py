"""Classify repos by how they depend on a target crate."""

import tomllib
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Classification:
    """Result of classifying a Cargo.toml's relationship to a target crate."""
    kind: str  # "direct" or "feature_flag"
    version: str = ""
    features: list[str] = field(default_factory=list)
    optional: bool = False
    default_features: bool = True
    parent_crate: str | None = None  # only for feature_flag


@dataclass
class RepoAnalysis:
    """A classified repository with its dependency chain."""
    repo: str
    classification: Classification | None
    chain: list[str]
    stars: int | None = None


@dataclass
class CategorizedEntry:
    """An entry in a categorized list."""
    repo: str
    chain: list[str]
    stars: int | None = None
    version: str = ""
    features: list[str] = field(default_factory=list)
    optional: bool = False
    default_features: bool = True

    def to_dict(self) -> dict:
        d = {"repo": self.repo, "chain": self.chain, "stars": self.stars}
        if self.version:
            d["version"] = self.version
        if self.features:
            d["features"] = self.features
        if self.optional:
            d["optional"] = self.optional
        if not self.default_features:
            d["default_features"] = self.default_features
        return d


def classify_cargo_toml(content: str, target_crate: str) -> Classification | None:
    """Classify how a Cargo.toml references a target crate."""
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return None

    # Check [dependencies], [dev-dependencies], [build-dependencies]
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        dep = data.get(section, {}).get(target_crate)
        if dep is not None:
            return _parse_direct_dep(dep)

    # Check [workspace.dependencies]
    ws_deps = data.get("workspace", {}).get("dependencies", {})
    dep = ws_deps.get(target_crate)
    if dep is not None:
        return _parse_direct_dep(dep)

    # Check if target_crate appears only in feature strings of other deps
    feature_flag_parent = _find_in_feature_flags(data, target_crate)
    if feature_flag_parent:
        return Classification(kind="feature_flag", parent_crate=feature_flag_parent)

    return None


def _parse_direct_dep(dep) -> Classification:
    """Parse a dependency value into a Classification."""
    if isinstance(dep, str):
        return Classification(kind="direct", version=dep)

    return Classification(
        kind="direct",
        version=dep.get("version", "workspace") if not dep.get("workspace") else "workspace",
        features=dep.get("features", []),
        optional=dep.get("optional", False),
        default_features=dep.get("default-features", True),
    )


def trace_chains(cargo_lock_content: str, target_crate: str) -> list[list[str]]:
    """Trace dependency chains from root crates to a target crate in a Cargo.lock.

    Returns a list of chains, where each chain is a list of crate names
    from a root crate to the target crate (inclusive).
    """
    packages = _parse_cargo_lock(cargo_lock_content)

    # Build reverse adjacency: child -> {parents}
    reverse_deps: dict[str, set[str]] = defaultdict(set)
    for pkg_name, pkg_info in packages.items():
        for dep_name in pkg_info["deps"]:
            reverse_deps[dep_name].add(pkg_name)

    roots = {name for name, info in packages.items() if info["source"] is None}

    if target_crate not in packages:
        return []

    chains: list[list[str]] = []
    _find_chains_to_roots(target_crate, reverse_deps, roots, [], chains, set())
    return chains


def _find_chains_to_roots(
    current: str,
    reverse_deps: dict[str, set[str]],
    roots: set[str],
    path: list[str],
    result: list[list[str]],
    visited: set[str],
):
    """DFS from target crate upward through reverse deps to find root crates."""
    if current in visited:
        return
    visited = visited | {current}

    path = [current] + path

    if current in roots:
        result.append(path)
        return

    parents = reverse_deps.get(current, set())
    if not parents:
        result.append(path)
        return

    for parent in sorted(parents):
        _find_chains_to_roots(parent, reverse_deps, roots, path, result, visited)


def _parse_cargo_lock(content: str) -> dict[str, dict]:
    """Parse a Cargo.lock file into a dict of package name -> info."""
    data = tomllib.loads(content)

    packages = {}
    for pkg in data.get("package", []):
        name = pkg["name"]
        deps = []
        for dep in pkg.get("dependencies", []):
            deps.append(dep.split()[0])
        packages[name] = {
            "version": pkg.get("version"),
            "source": pkg.get("source"),
            "deps": deps,
        }
    return packages


def _find_in_feature_flags(data: dict, target_crate: str) -> str | None:
    """Check if target_crate appears in feature flags of another dep."""
    all_deps = list(data.get("dependencies", {}).items())
    all_deps += list(data.get("dev-dependencies", {}).items())
    all_deps += list(data.get("build-dependencies", {}).items())
    all_deps += list(
        data.get("workspace", {}).get("dependencies", {}).items()
    )

    for crate_name, dep_val in all_deps:
        if not isinstance(dep_val, dict):
            continue
        for feat in dep_val.get("features", []):
            if feat == target_crate or f"-{target_crate}" in feat or f"{target_crate}/" in feat:
                return crate_name

    return None


def categorize(
    repos: list[RepoAnalysis], target_crate: str
) -> dict[str, list[CategorizedEntry]]:
    """Categorize repos into lists named after direct dependants."""
    result: dict[str, list[CategorizedEntry]] = defaultdict(list)

    for repo in repos:
        entry = CategorizedEntry(
            repo=repo.repo, chain=repo.chain, stars=repo.stars
        )

        if repo.classification and repo.classification.kind == "direct":
            entry.version = repo.classification.version
            entry.features = repo.classification.features
            entry.optional = repo.classification.optional
            entry.default_features = repo.classification.default_features
            result["direct"].append(entry)
            continue

        # Use the chain to find which direct dependant of target_crate
        # this repo goes through.
        if target_crate in repo.chain:
            idx = repo.chain.index(target_crate)
            if idx > 0:
                direct_parent = repo.chain[idx - 1]
                result[direct_parent].append(entry)
                continue

        result["unknown"].append(entry)

    return dict(result)
