# Pubky Dependents Analysis

**[Live dashboard](https://its-gaib.github.io/pubky-dependents-analysis/)**

Automated weekly analysis of which projects depend on Pubky ecosystem crates, how they depend on them, and through which intermediary.

## API

Each analyzed crate has a JSON endpoint:

```
https://its-gaib.github.io/pubky-dependents-analysis/{crate}.json
```

Available: `pkarr.json`, `pubky.json`, `pubky-app-specs.json`

## JSON Schema

```jsonc
{
  "crate": "pkarr",                    // analyzed crate name
  "updated_at": "2026-04-06T...",      // ISO 8601 timestamp of last analysis
  "collection": {                    // present on validated new publications
    "status": "complete",
    "run_id": "2026-04-06T...",       // shared UTC start timestamp for all crates
    "sources": {                     // successful discovery counts by source
      "crates_io": 10,
      "github_cargo_toml": 90,
      "github_cargo_lock": 200,
      "github_dependents": 180,
      "npm_registry": 20,            // only when npm_package is configured
      "github_package_json": 30
    }
  },
  "total": 281,                        // total number of classified dependant repos
  "summary": {                         // repo count per category
    "direct": 30,                      // repos that list this crate in their Cargo.toml
    "iroh": 202,                       // repos that get this crate transitively through "iroh"
    "pubky": 6                         // repos that get this crate transitively through "pubky"
  },
  "lists": {                           // full data per category
    "direct": [
      {
        "repo": "fedimint/fedimint",   // GitHub owner/name
        "chain": ["fedimint-server", "pkarr"],  // dependency chain from repo's crate to target
        "stars": 680,                  // GitHub star count (null if unavailable)
        "version": "3.10.0",          // pkarr version requirement (direct deps only)
        "features": ["dht", "relays"] // enabled pkarr features (direct deps only)
      }
    ],
    "iroh": [
      {
        "repo": "moq-dev/moq",
        "chain": ["moq-cli", "moq-native", "web-transport-iroh", "iroh", "pkarr"],
        "stars": 1090
      }
    ]
  },
  "crates_io_downloads": {             // crates.io download stats (if published)
    "total": 611326,                   // all-time downloads (includes private/CI usage)
    "recent": 214362                   // downloads in the last 90 days
  },
  "npm_downloads": {                   // npm download stats (if npm_package configured)
    "total": 8871,                     // all-time downloads
    "recent": 1050                     // downloads in the last 30 days
  },
  "npm_dependents": [...]              // explicit array, including [], when npm is configured
}
```

## How to interpret `lists`

- **`"direct"`**: Projects that have the target crate as an explicit dependency in their `Cargo.toml`.
- **Any other key** (e.g. `"iroh"`, `"pubky"`): Projects where the target crate is a *transitive* dependency. The key name is the crate that directly depends on the target. Read the `chain` array left-to-right as the dependency path from the project's own crate down to the target.

Example: `"chain": ["moq-cli", "moq-native", "web-transport-iroh", "iroh", "pkarr"]` means moq-cli depends on moq-native, which depends on web-transport-iroh, which depends on iroh, which depends on pkarr.

## Limitations

This analysis only tracks **public / open-source projects**. Private and proprietary projects that depend on these crates are not visible through GitHub's dependency graph or code search. The `crates_io_downloads` field provides a rough indicator of total adoption (public + private), since download counts include all usage — CI pipelines, proprietary builds, etc.

`collection.status: "complete"` means the configured discovery requests and their validation succeeded. It does not guarantee completeness of GitHub's search index. The npm metric retains the existing sample of the first 50 npm keyword-search results and first 50 GitHub `package.json` matches, with the existing deduplication. Keyword matches are not independently verified dependency declarations. Its two source counts describe the included entries, so their sum equals `npm_dependents.length`. Rust source counts describe discoveries before merging and classification and do not sum to `total`.

## Failed collections and recovery

The weekly analysis starts on Monday at 06:00 UTC. Three recovery opportunities are scheduled at Monday 12:00, Monday 18:00, and Tuesday 00:00 UTC; actual GitHub Actions starts can be delayed. Once every configured crate has a validated publication from the current Monday cycle with the same `collection.run_id`, later opportunities skip analysis. They still upload and deploy the committed snapshot, allowing recovery from a failed Pages deployment. A manual **Analyze Dependents** run on `main` can retry sooner; after the final scheduled recovery, the next automatic opportunity is the following Monday.

GitHub requests retry rate limits/timeouts at most four times, with delays of 30, 60, and 120 seconds. Dependents-page requests try at most three times. A workflow run is limited to 330 minutes, and a later recovery does not cancel an active analysis. Discovery failures, incomplete or truncated search responses, the GitHub 1,000-result search cap, broken dependents pagination, and classification transport errors fail the run. Optional star/download statistics can remain unavailable without invalidating dependent counts.

All configured crate outputs are staged and validated before replacing any files in `docs/`; only a successful full batch is committed and deployed. `updated_at` records each crate's actual measurement completion and remains unchanged after a failed attempt. Downstream collectors should retain it, require a common run identity, and label old measurements rather than mistaking a new fetch for a new analysis. Existing JSON without `collection` is legacy data with unverified completeness; this change does not rewrite historical counts.

A drop of more than 25% and at least 20 in a crate's classified total, npm sample, or previously recorded source count blocks publication. This is an anomaly guard, not a rule that adoption cannot decrease. Inspect the source failure and run logs first. For an independently verified genuine decline, run **Analyze Dependents** manually on `main` with **allow_large_decrease** enabled (locally: `python analyze.py --allow-large-decrease`). This bypasses only the decline guard; source completeness and schema validation still apply. A filtered local run (`python analyze.py pkarr`) is useful for investigation but cannot pass the full-batch publication check alongside snapshots from a different run.

## Development checks

Python 3.14 is recommended and selected by `.python-version` for the scheduled analyzer and lint job. Python 3.11–3.14 remains supported and tested. Node.js 24 LTS is selected by `.nvmrc` for the dashboard checks; run `nvm use` if you use nvm. CI selects the latest patch release within each configured version.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip check
python -m ruff check .
python -m ruff format --check .
python -m pytest -v
node --test tests/dashboard.test.cjs
```

The tests run offline using saved Cargo fixtures and mocked HTTP/CLI responses. They cover classification, analysis output, source error handling, published JSON consistency, and dashboard rendering. The dashboard tests use Node's built-in test runner and require no npm dependencies.

Lint and Tests run on pull requests targeting `main` and pushes to `main`, with read-only repository permissions. CI checks Ruff lint/format, tests Python 3.11–3.14, runs the dashboard checks, and validates all GitHub Actions workflows and their shell scripts with actionlint 1.7.12. To run the workflow check locally with Docker:

```sh
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.12 -color
```

Pages deploys the checked commit only after both workflows pass. The scheduled analysis also validates its generated JSON before committing and publishing it.

## Dependency maintenance

`requirements.txt` pins Requests and its runtime dependencies. `requirements-dev.txt` includes those pins plus pytest, Ruff, and their dependencies. Install the runtime file to run the analyzer locally, or the development file to run the checks. The scheduled job installs the development file because it validates generated data with pytest before publishing.

For dependency updates, check available releases with `python -m pip list --outdated`, update the relevant pins in both requirements files, and run the development checks above in a fresh virtual environment. Keep the Windows-only `colorama` marker when updating pytest's dependencies.

Review `.python-version` and `.nvmrc` when adopting a new stable Python or Node LTS release, and keep the Python test matrix aligned with the supported versions. Check GitHub Actions and actionlint releases when updating CI tooling; update the actionlint image in the workflow and local command together.
