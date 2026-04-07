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
  "npm_dependents": [...]              // npm packages referencing this crate (if applicable)
}
```

## How to interpret `lists`

- **`"direct"`**: Projects that have the target crate as an explicit dependency in their `Cargo.toml`.
- **Any other key** (e.g. `"iroh"`, `"pubky"`): Projects where the target crate is a *transitive* dependency. The key name is the crate that directly depends on the target. Read the `chain` array left-to-right as the dependency path from the project's own crate down to the target.

Example: `"chain": ["moq-cli", "moq-native", "web-transport-iroh", "iroh", "pkarr"]` means moq-cli depends on moq-native, which depends on web-transport-iroh, which depends on iroh, which depends on pkarr.

## Limitations

This analysis only tracks **public / open-source projects**. Private and proprietary projects that depend on these crates are not visible through GitHub's dependency graph or code search. The `crates_io_downloads` field provides a rough indicator of total adoption (public + private), since download counts include all usage — CI pipelines, proprietary builds, etc.
