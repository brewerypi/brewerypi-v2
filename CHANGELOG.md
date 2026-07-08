# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project aims to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Renamed `tag_values.timestamp` to `observed_at` (avoids the reserved word
  and follows the `_at` convention) via a data-preserving Alembic migration
  (`ba9dc47dbd8c`, batch column rename). MCP tool inputs and outputs use
  `observed_at` to match the column. New readings now default their time to
  `datetime.now(timezone.utc)` (UTC by design, not by server-clock luck).

### Added
- Service-layer CRUD for element templates
  (`services/element_templates.py`): list/get/create/update/delete.
  Create/update validate name uniqueness within the site and that a parent
  belongs to the same site; re-parenting enforces no cycles (a template can't
  become its own ancestor). Update can re-parent (int), promote to top-level
  (`None`), or leave the parent unchanged (omit). Delete refuses when the
  template has children. Covered by
  `tests/test_services_element_templates.py`.
- `ElementTemplate` model and create-table migration (`223ac0a8de06`): a
  site-scoped, self-referential template tree (`site_id`, nullable `parent_id`,
  `name`, `description`; name unique within the site). `Site` cascade-deletes
  its element templates. This is the first migration that creates a new table.
- Admin MCP tools for enterprises: `get_enterprise`, `create_enterprise`,
  `update_enterprise`, `delete_enterprise` (admin tier now 39 tools). Listing
  reuses the operator `list_enterprises`. `delete_enterprise` previews the full
  subtree (site/area/tag/lookup/measurement-unit counts), requires
  `confirm=true`, and is refused when readings exist under the enterprise. This
  completes config CRUD for all seven tables. Covered by
  `tests/test_mcp_config_tools_enterprises.py`.
- Service-layer CRUD for enterprises (`services/enterprises.py`), the top of
  the hierarchy. Create/update validate global uniqueness of abbreviation and
  name; delete refuses if any recorded reading exists under its sites, or (as
  a defense-in-depth safety net) if any of its lookup values are referenced by
  a reading — either would destroy or block on history when the whole subtree
  cascades. Covered by `tests/test_services_enterprises.py`.
- Admin MCP tools for sites: `get_site`, `create_site`, `update_site`,
  `delete_site` (admin tier now 35 tools). Listing reuses the operator
  `list_sites`. `delete_site` previews with the area and tag counts, requires
  `confirm=true`, and is refused when readings exist below the site. Covered by
  `tests/test_mcp_config_tools_sites.py`.
- Service-layer CRUD for sites (`services/sites.py`). Create/update validate
  per-enterprise uniqueness of abbreviation and name; delete refuses when any
  recorded reading exists under the site (Site -> areas -> tags -> tag_values
  all cascade), so a structural delete can't silently destroy history. Covered
  by `tests/test_services_sites.py`.
- Operator MCP tools for correcting readings: `get_tag_value`,
  `update_tag_value` (value and/or observed_at), and `delete_tag_value`
  (`confirm=true`), on the operator tier so operators can fix their own
  mis-entries without an admin. `get_tag_values` now includes each reading's
  `id` so a reading can be targeted. Covered by
  `tests/test_mcp_tag_value_tools.py`.
- Service-layer read/update/delete for tag values (`services/tag_values.py`):
  `get_tag_value`, `update_tag_value` (corrects a reading's value and/or
  observed time, enforcing the tag's numeric-vs-lookup type so the XOR invariant
  holds), and `delete_tag_value`. Creation stays with the operator-tier
  `record_tag_value`; these are corrective operations on the operator tier.
  Covered by `tests/test_services_tag_values.py`.
- Admin MCP tools for areas: `get_area`, `create_area`, `update_area`,
  `delete_area` (admin tier now 28 tools). Listing reuses the operator
  `list_areas`. `delete_area` previews with the tag count, requires
  `confirm=true`, and is refused when readings exist below the area. Covered by
  `tests/test_mcp_config_tools_areas.py`.
- Service-layer CRUD for areas (`services/areas.py`), plus an `optional_str`
  helper in `_validation.py`. Create/update validate per-site uniqueness of
  abbreviation and name; delete refuses when any recorded reading exists under
  the area's tags (Area -> tags -> tag_values all cascade), so a structural
  delete can't silently destroy history. Covered by
  `tests/test_services_areas.py`.
- Documented the MCP tool naming convention in `CONVENTIONS.md` (with a
  pointer from `CLAUDE.md`): admin tools never shadow operator tools; hierarchy
  tables reuse the operator `list_<table>` and add `get_<table>`, reference
  tables own `list_<table>`; destructive tools require `confirm=true`.
- Admin MCP tools for tags: `get_tag`, `create_tag`, `update_tag`,
  `delete_tag` (admin tier now 24 tools). Listing reuses the operator
  `list_tags`; `get_tag` returns the raw config fields (lookup_id,
  measurement_unit_id) for editing. `delete_tag` previews, requires
  `confirm=true`, and is refused when the tag has readings. Covered by
  `tests/test_mcp_config_tools_tags.py`.
- Service-layer CRUD for tags (`services/tags.py`). Create validates the area,
  per-area name uniqueness, that a tag is either lookup-typed or numeric (not
  both), and that any referenced lookup or measurement unit belongs to the
  tag's own enterprise; delete refuses when the tag has recorded readings
  (which would otherwise cascade-delete history). Update covers name and
  description only. Covered by `tests/test_services_tags.py`.
- Admin MCP tools for lookups and lookup values (`list`/`create`/`update`/
  `delete` each), wrapping the service layer and registered on the admin tier
  next to the measurement-unit tools (admin tier is now 20 tools). Destructive
  deletes preview and require `confirm=true`. Covered by
  `tests/test_mcp_config_tools_lookups.py`.
- Service-layer CRUD for lookups and lookup values
  (`services/lookups.py`, `services/lookup_values.py`), plus a shared
  `clean_str` validator (`services/_validation.py`, now also used by
  measurement_units). Lookup delete refuses when a tag uses the lookup or a
  recorded reading references one of its values; lookup-value delete refuses
  when a reading references it. Covered by `tests/test_services_lookups.py`.
- Admin MCP tier: config-editing tools are gated by `MCP_ROLE=admin` and
  registered only on that tier, run as a separate process on its own port and
  secret path. First config CRUD exposed is measurement units
  (`list`/`create`/`update`/`delete_measurement_unit`, wrapping the service
  layer), with destructive deletes requiring an explicit `confirm=true` after
  a preview. Covered by `tests/test_mcp_config_tools.py`. Deploy guide gains
  the admin endpoint and the Alembic adoption/upgrade steps.
- Service layer (`src/brewerypi/services/`): reusable business logic shared by
  the MCP tools and future consumers, with a `ServiceError` hierarchy
  (`NotFoundError`/`ValidationError`/`ConflictError`). First slice is
  measurement-unit CRUD (`create`/`get`/`list`/`update`/`delete`) with
  enterprise-existence and per-enterprise uniqueness validation and a delete
  guard that refuses to remove a unit still referenced by tags. Covered by
  `tests/test_services_measurement_units.py`.
- Alembic migrations (`migrations/`) with an initial migration capturing the
  current schema, wired to `Base.metadata` and `DATABASE_URL`. Adds a
  `migrations` optional dependency (`alembic`); versioned schema changes now go
  through Alembic rather than `create_all`.
- `record_tag_value` write tool on the MCP server — appends a single reading
  to a tag, validating numeric vs lookup-typed tags and (for lookup tags)
  that the value is selectable. SQLite engine now enables `foreign_keys` and a
  busy-timeout for safe concurrent writes. **Auth is unchanged (one shared
  secret path), so anyone with the URL can write** — acceptable only because
  the demo database is a rebuildable throwaway.
- Read-only MCP server (`src/brewerypi/mcp_server.py`) exposing seven tools
  over the hierarchy and time series (`list_enterprises`, `list_sites`,
  `list_areas`, `list_tags`, `get_tag_values`, `tag_value_stats`,
  `browse_hierarchy`), served over streamable HTTP. Adds an optional `mcp`
  dependency group (`fastmcp`) and a `brewerypi-mcp` console script. Tests in
  `tests/test_mcp_server.py` cover the tools (CI installs the `mcp` extra to
  run them), and `docs/deploy-mcp-hetzner.md` documents deploying it on a
  Hetzner VPS behind Caddy with a secret-path gate.
- SQLAlchemy 2.0 models for `MeasurementUnit`, `Tag`, and `TagValue`.
  `MeasurementUnit` is enterprise-scoped (abbreviation, name, description).
  `Tag` belongs to `Area` with nullable `lookup_id` and `measurement_unit_id`
  FKs; `lookup_id` presence distinguishes lookup-typed from numeric tags.
  `TagValue` uses two nullable columns (`value` Float for numeric tags,
  `lookup_value_id` FK for lookup-typed tags) with a `CHECK` constraint
  (`ck_tag_values_value_xor_lookup_value_id`) enforcing exactly one is
  non-null. `lookup_value_id` carries `ON DELETE RESTRICT` to protect
  historical data; `LookupValue.tag_values` sets `passive_deletes=True` so
  the database — not the ORM — enforces the restriction.
- Tests for the new models covering navigation, cascade deletes, the XOR
  constraint, and `RESTRICT` enforcement; `PRAGMA foreign_keys=ON` now
  enabled in the test engine.
- Seed data now includes four measurement units (°C, °P, bar, pH) per
  enterprise.
- SQLAlchemy 2.0 models for `Lookup` and `LookupValue`, extending the
  hierarchy as `Enterprise 1——* Lookup 1——* LookupValue`; includes cascade
  deletes, composite unique constraints with distinct names, and a reverse
  `lookups` relationship on `Enterprise`.
- Tests for `Lookup`/`LookupValue` navigation and cascade delete added to
  `tests/test_models.py`.
- Project skeleton: `src/` layout, `pyproject.toml` packaging with a
  `brewerypi` console script, example tests, and a sample-data seed script.
- SQLAlchemy 2.0 models for the top of the BreweryPi equipment hierarchy —
  `Enterprise`, `Site`, `Area` — on a `Base` preconfigured with a constraint
  and index naming convention.
- `CONVENTIONS.md` documenting the naming standard for both database
  identifiers and project files.
- PEP 8 enforcement with ruff (line length 79; `E`/`W`/`F`/`I` rules) plus a
  pre-commit hook that runs it on every commit.
- Continuous integration: a GitHub Actions workflow running ruff and pytest on
  every push and pull request across Python 3.10–3.13.
- `LICENSE` (MIT) for the project.
- `CLAUDE.md` project-context file so Claude Code loads the project's
  decisions and conventions automatically.
- `.gitattributes` normalizing line endings to LF across platforms.

### Changed
- Expanded `.gitignore` to cover virtual environments, type-check and coverage
  caches, editor/OS files, and `CLAUDE.local.md`.
- README now links to the `LICENSE` file and notes the upstream attribution.
