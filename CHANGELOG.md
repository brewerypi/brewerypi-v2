# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project aims to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
