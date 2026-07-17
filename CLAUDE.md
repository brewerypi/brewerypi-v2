# Brewery Pi — Project Context

Persistent context for Claude Code. Brewery Pi is a process-data historian for
breweries (originally Raspberry Pi-based). This repo modernizes the existing
BreweryPi project (github.com/brewerypi/brewerypi, MIT), which the maintainer
owns and has rights to. Keep the name `brewerypi` everywhere.

## Stack & layout
- Python 3.10+, `src/` layout, import package `brewerypi`.
- SQLAlchemy 2.0 ORM style (`DeclarativeBase`, `Mapped`, `mapped_column`).
- SQLite for local dev via `DATABASE_URL` in `src/brewerypi/config.py`
  (default `sqlite:///app.db`); designed to swap to Postgres/Turso later.
- Consumers: an MCP server (`src/brewerypi/mcp_server.py`) with two tiers,
  selected by `MCP_ROLE`: the default **operator** tier (browse/query + one
  write tool `record_tag_value`), and an **admin** tier (`MCP_ROLE=admin`)
  that also exposes config CRUD tools (measurement units so far) wrapping the
  service layer. The tiers run as separate processes on separate ports/secret
  paths; built and deployed for demos. A Flask web app is still planned.
- `pyproject.toml` is the single source of build, dependency, and tool config.

## Commands
- Install (editable + dev tools): `pip install -e ".[dev]"`
- Run / initialize the database: `python -m brewerypi.main`
- Seed sample data: `python scripts/seed_sample_data.py`
- Tests: `pytest`
- Lint (PEP 8): `ruff check .`  (auto-fix: `ruff check . --fix`)
- Enable the commit hook: `pre-commit install`
- Run the MCP server: `pip install -e ".[mcp]"` then `brewerypi-mcp`
- Migrations (Alembic): `pip install -e ".[migrations]"`, then
  `alembic upgrade head` (new DB) or `alembic stamp head` (adopt on an
  existing create_all DB); `alembic revision --autogenerate -m "..."` for
  schema changes.
  (env: `MCP_HOST`/`MCP_PORT`/`MCP_PATH`, `DATABASE_URL`). Deploy guide:
  `docs/deploy-mcp-hetzner.md`.

## Conventions (full detail in CONVENTIONS.md)
- Tables: plural `snake_case` (`enterprises`, `sites`, `areas`).
- Columns: `snake_case`; primary key `id`; foreign key `<parent>_id`.
- Timestamps end in `_at`; booleans start with `is_`/`has_`; measurement
  columns carry their unit (`total_cents`, `temperature_c`).
- Constraint names come from the naming convention on `Base.metadata` in
  `database.py` (`pk_`/`fk_`/`uq_`/`ix_`/`ck_`). Because the `uq` name keys off
  the FIRST column, order columns inside a composite `UniqueConstraint` so the
  two constraints on a table get distinct names (see `Site` in `models.py`).
- Files: `snake_case.py` modules; ALL-CAPS root meta files; lowercase
  `kebab-case` docs under `docs/`.
- PEP 8 enforced by ruff at line-length 79 (configured in `pyproject.toml`).
- MCP tool naming (see CONVENTIONS.md): admin tools never shadow operator
  tools. Writes are uniform `create_/update_/delete_<table>`. Reads: hierarchy
  tables (enterprise/site/area/tag) reuse the operator `list_<table>` and add
  `get_<table>`; reference tables (measurement_units/lookups/lookup_values)
  own `list_<table>`. Destructive tools take `confirm=true`.

## Current models (src/brewerypi/models.py)
- Equipment hierarchy: `Enterprise` 1—* `Site` 1—* `Area` 1—* `Tag` 1—*
  `TagValue` (ISA-95 style). `Enterprise` also owns `Lookup` (1—*
  `LookupValue`) and `MeasurementUnit`. `Tag.name` is `String(255)` (unlike the
  45-char names elsewhere) so it can hold generated element-attribute tag
  paths like `Cellar.FV01.Temperature`; unique within its area.
- `Enterprise`/`Site`/`Area` each have `id`, `abbreviation` (10), `name` (45),
  `description` (255, nullable), a parent FK with `index=True`, and composite
  unique constraints (name/abbreviation unique within the parent). Cascade.
- `Tag` belongs to `Area` with nullable `lookup_id` and `measurement_unit_id`;
  `lookup_id` presence marks a lookup-typed tag vs a numeric one.
- `TagValue` is the time-series table: an `observed_at` column (the reading's
  time, UTC — new writes default to `datetime.now(timezone.utc)`) plus two
  nullable value columns — `value` (Float) for numeric tags, `lookup_value_id`
  for lookup-typed — with a `CHECK` enforcing exactly one is set.
  `lookup_value_id` uses `ON DELETE RESTRICT` + `passive_deletes=True` so
  historical readings can't be silently deleted. This is the high-volume table
  that motivates the
  eventual Postgres/Timescale move.
- Timezones: readings are stored UTC; `Site.timezone` (IANA) is the display/
  entry zone. `brewerypi/timezones.py` converts at the tool boundary
  (`to_utc`/`from_utc`, DST-aware via `zoneinfo`); `resolve_timezone(session,
  site)` is the seam that returns the site's zone now and the authenticated
  user's once OAuth lands (driving both entry and display). The model never
  does timezone math.
- `ElementTemplate`: site-scoped, self-referential template tree — `site_id`,
  nullable `parent_id` (NULL = top-level, e.g. a Brewhouse with Mash Tun /
  Lauter Tun children), `name` (unique per site), `description`, and
  `exclusive` (bool, default True) marking its elements single-occupancy for
  event frames (True = no overlapping frames; False = umbrella like a
  brewhouse). `Site` cascade-deletes its templates. Service delete refuses if
  a template has children (leaf-upward).
- `Element`: an instance of an `element_template` (e.g. FV01/FV02 of a
  Fermenter). Required immutable `element_template_id`, nullable `tag_area_id`
  (→ `areas`, where its tags get stored, assignable later), nullable self-FK
  `parent_id`; `name`, `description`. Its parent tree mirrors the template tree
  (A1: parent instances the template's parent template — service-enforced),
  and tag_area/parent must be same-site. Unique `(parent_id, name)`; roots
  unique within the template (service-level). `ElementTemplate` cascades its
  elements (so a site delete unwinds cleanly). Operators read; admins write.
- `ElementAttributeTemplate`: defines an attribute on an `element_template`
  (e.g. Temperature/Pressure on a Fermenter). Required `element_template_id`,
  `name` (unique per template), `description`, and nullable
  `lookup_id`/`measurement_unit_id` — mutually exclusive (lookup/numeric/
  neither, like Tag), same-enterprise (service-enforced). Flat (no parent).
  `ElementTemplate` cascades its attribute templates. Admin-only config. A
  future `ElementAttribute` instance will carry the tag_id link.
- `ElementAttribute`: an attribute template realized on one element and wired
  to a `Tag` (required `element_id`, `element_attribute_template_id`, `tag_id`;
  unique `(element_id, element_attribute_template_id)`). `owns_tag` (bool)
  records provenance: True = the app auto-created the tag for this attribute
  (removed with the attribute, if it has no readings); False = an existing tag
  was adopted by name (only the link is removed). `tag_id` is `ON DELETE
  RESTRICT` + `passive_deletes`, so a wired tag can't be deleted out from
  under an attribute. `Element` and `ElementAttributeTemplate` cascade their
  attributes; deleting an element unwires its attributes but leaves tags and
  readings intact.
- Element attribute WIRING lives in `services/element_attributes.py`. Tag names
  are the element path + attribute (`Cellar.FV01.Temperature`, separator from
  `TAG_PATH_SEPARATOR`). Find-or-create: create the tag (`owns_tag=True`) or
  adopt a same-named one if its type matches (`owns_tag=False`; mismatch =
  error; shared adopted tags allowed). Wiring fires on element create, on
  tag-area assignment, and retroactively when an attribute template is added.
  Rename/re-parent resyncs owned tag names across the descendant subtree.
  Unwire refuses when an owned tag has readings.
- `EventFrameTemplate` (event frames, in progress): a batch-window type defined
  for an `element_template` (`element_template_id`), self-referential
  (`parent_id`) so a "Brew" on a Brewhouse nests a "Mashing" on the Mash Mixer
  child. Name unique per element template. `ElementTemplate` cascades them. A1
  nesting mirror + `default_start/end` attribute values + the instance side
  (overlap guard via `element_template.exclusive`, containment, open/close/
  reopen, tag wiring reuse) are being built table-by-table.
- Derived from upstream BreweryPi's schema; tested in `tests/test_models.py`.

## OPEN DECISION — schema naming (parked; decide before renaming anything)
Our conventions rename upstream's existing identifiers — e.g. table `Enterprise`
with PK `EnterpriseId` and FK `SiteId` — to `enterprises` / `id` / `enterprise_id`.
- For NEW tables this is free; apply the conventions.
- For the EXISTING tables it is a BREAKING change: live BreweryPi deployments
  have the old names, so renaming requires an Alembic migration + a major
  version (v2) bump.
- Modernization splits in two: the `src/` layout, SQLAlchemy 2.0 API,
  ruff/pre-commit, and conventions-for-new-tables are all NON-breaking and safe
  to adopt now. The 2.0 API works fine with the OLD names too.
- IMPORTANT: `models.py` currently uses the NEW convention names — i.e. it
  encodes the v2/greenfield path. If backward compatibility is required, the
  existing models must instead keep the original names
  (`__tablename__ = "Enterprise"`, `EnterpriseId`, …).
- Two resolutions when resumed: (a) preserve existing names (non-breaking, map
  2.0 models onto current databases), or (b) commit to v2 with an Alembic
  migration that renames tables/columns.

## Gotchas
- Re-running the seed script on a populated database fails (unique constraints
  on enterprise name/abbreviation). Clear the DB first — delete `app.db`, or
  drop and recreate the tables — then seed.
- For the MCP-server path, set `DATABASE_URL` to an ABSOLUTE sqlite path, since
  the server is launched from a different working directory than the repo root.
- Line endings: `.gitattributes` pins `* text=auto eol=lf`; keep the editor on LF.

## Not done yet
- `create_all` (in `main.py`) still builds the schema for the throwaway
  demo DB; Alembic is now the mechanism for versioned schema changes. Adopt
  Alembic on the existing server DB with `alembic stamp head`.
- Add a branch-protection ruleset on GitHub requiring the CI checks to pass
  before merging to `main` (repo is pushed; ruleset not yet configured).
- `brewerypi` is unclaimed on PyPI; reserve it if you plan to publish.
- Repo split (e.g. `brewerypi-db`) intentionally deferred until a second
  consumer exists and the schema stabilizes.
- Schema-naming decision (see above) is still open.

## In place
- MIT `LICENSE`, CI (GitHub Actions: ruff + pytest on 3.10–3.13, installing
  the `dev` + `mcp` extras), pre-commit hook, `.gitattributes` (LF).
- Pushed to GitHub at `github.com/brewerypi/brewerypi-v2`.
- Alembic migrations in `migrations/` (initial migration captures the current
  schema; `env.py` wired to `Base.metadata` + `DATABASE_URL`).
- Service layer (`src/brewerypi/services/`): reusable CRUD/business logic
  shared by MCP tools and future consumers; raises a `ServiceError` hierarchy;
  callers own the Session/transaction. Module per table:
  `measurement_units`, `lookups`, `lookup_values`, `tags`, `areas`, `sites`,
  `enterprises`, `element_templates`, `elements`,
  `element_attribute_templates`, `event_frame_templates` (shared `clean_str` /
  `optional_str` /
  `clean_name_segment` in `_validation.py`; `clean_name_segment` is used for
  element and attribute-template names — trims, collapses internal whitespace,
  rejects the `.` tag-path separator — since they become segments of generated
  tag names like `Cellar.FV01.Temperature`).
  `elements` enforces the A1 mirror rule +
  same-site tag_area; its guards also extended `delete_element_template`
  (refuse if instances) and `delete_area` (refuse if used as a tag area).
  `element_attribute_templates` reuses the Tag type-pattern (lookup/numeric/
  neither, same-enterprise) and extended `delete_lookup` /
  `delete_measurement_unit` (refuse if an attribute template references them);
  admin owns its `list_`/`create`/`update`/`delete` tools.
  Operators read elements; admins write. MCP tools: operator tier 17
  (element reads, element-attribute reads, and event-frame-template reads —
  operators browse batch types to start instances), admin tier 61 (adds
  `create`/`update`/`delete_element`, the config tables' CRUD, the element
  attribute template tools, `wire`/`unwire_element_attribute`, and
  `create`/`update`/`delete_event_frame_template`).
  `element_templates` is a config table the operator tier doesn't
  browse, so admin OWNS `list_element_templates` (reference-table pattern);
  its update re-parents via `parent_id` / promotes via `make_top_level`.
  Hierarchy deletes guard on readings in the subtree; hierarchy tables' admin
  read reuses operator `list_<table>` + adds `get_<table>`.
- `tag_values` (historian data, not config): create = operator
  `record_tag_value`; read = operator `get_tag_values` (now includes reading
  `id`). Operators also get corrective `get_tag_value` / `update_tag_value` /
  `delete_tag_value` (update enforces the tag's numeric/lookup type; delete
  takes `confirm=true`) — on the operator tier so they can fix their own
  mis-entries.
- MCP server built, tested (`tests/test_mcp_server.py`), and deployed for a
  demo: Hetzner VPS + Caddy (HTTPS), added as a custom connector behind a
  secret-path gate. All tools read-only except `record_tag_value` (write);
  single shared secret = anyone with the URL can write, acceptable only
  because the demo DB is a rebuildable throwaway. Guide:
  `docs/deploy-mcp-hetzner.md`.
