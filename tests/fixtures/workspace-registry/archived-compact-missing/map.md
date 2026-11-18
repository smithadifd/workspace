# Fixture workspace — orientation map

A launchpad, not a project. This file maps the repos under the code roots.

## Repos by cluster

**Core systems** (owner: platform)
- `alpha`: Primary service. owner: platform. owned. lifecycle: active. doc: docs/alpha.md. confused_with: `alpha-client` (service, not client).

## Relationships (who depends on whom)

- `alpha` → `legacy`: protocol: reads the legacy export format.
