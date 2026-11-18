# Example workspace — orientation map

A launchpad, not a project. This file maps the repos under `~/src`.

## Repos by cluster (all under ~/src)

**Storefront** (owner: web team)
- `shop-web`: Storefront web application. owner: web team. owned. lifecycle: active. confused_with: `shop-api` (the browser app, not the API).
- `shop-api`: Storefront API. owner: web team. owned. lifecycle: maintenance.
- `ingest`: Nightly ingest pipeline. owner: data team. owned. lifecycle: active. doc: docs/ingest.md.

**Data** (owner: data team)
- `ingest`: Nightly ingest pipeline. owner: data team. owned. lifecycle: active. doc: docs/ingest.md.
- `reports`: Weekly reports. owner: data team. reference. lifecycle: active.

**Retired systems** (lifecycle: archived): `old-cart`. Systems now owned elsewhere.

## Relationships (who depends on whom)

- `shop-web` → `shop-api`: runtime: SHOP_API_URL.
- `reports` → `ingest`: source: reads ingest/out tables.
