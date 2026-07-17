# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project aims to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `EventFrameTemplate` model and create-table migration (`76ca67faa837`): a
  type of event frame (batch window) defined for an element template
  (`element_template_id`), self-referential (`parent_id`) so a "Brew" template
  on a Brewhouse can nest a "Mashing" child on the Brewhouse's Mash Mixer
  child. Name unique per element template. `ElementTemplate` cascades its
  event frame templates (site teardown of the whole tree verified). The A1
  nesting mirror rule lands with the service layer next.
- `element_templates.exclusive` flag (bool, migration `324d3d5cb416`,
  backfilled to True): marks elements of the template as single-occupancy for
  event frames — when True, event frames on such an element may not overlap in
  time (across any template); False allows unlimited concurrency (umbrella
  equipment like a brewhouse). Settable via the element-template service and
  admin tools. Foundation for the event-frame overlap guard. Covered by
  `tests/test_services_element_templates.py`.

### Changed
- Reading tools now convert at the timezone boundary. `record_tag_value` and
  `update_tag_value` interpret an incoming `observed_at` as the site's local
  time and store UTC; `get_tag_values` and `tag_value_stats` interpret their
  `start`/`end` filters as local, and reads return `observed_at` in local time
  with the resolved `timezone`. The zone is resolved per reading via
  tag → area → site → `resolve_timezone`, the same seam OAuth will later point
  at the user. The service layer stays pure UTC; all conversion is
  deterministic (`zoneinfo`) at the tool boundary. Covered by
  `tests/test_mcp_reading_timezone.py`.

### Added
- Timezone foundation. `sites` gain an IANA `timezone` column (migration
  `4408e06fe84b`, backfilled to "UTC"), validated on create/update against
  `zoneinfo`. New `brewerypi/timezones.py` holds the deterministic,
  DST-aware conversion shell — `to_utc`/`from_utc` (readings stay UTC; local
  times convert at the boundary) and `resolve_timezone`, the single seam that
  today returns the site's zone and will prefer the authenticated user's once
  OAuth lands (driving both entry and display). Site admin tools take a
  `timezone` argument. Reading tools are wired to it in a follow-up. Covered by
  `tests/test_timezones.py`.
- Element attribute MCP tools, reads-operator / writes-admin: operator
  `list_element_attributes` (filter by element; each row carries the attribute
  name plus the `tag_id`/`tag_name` holding its data) and
  `get_element_attribute`; admin `wire_element_attribute` (manual wiring, incl.
  linking an existing tag) and `unwire_element_attribute` (previews, then
  `confirm=true`; refused when an owned tag has readings). Operator tier now 15
  tools, admin 56. Completes the element attribute feature. Covered by
  `tests/test_mcp_element_attribute_tools.py`.
- Element attribute wiring (`services/element_attributes.py`): generates tag
  names from the element's path plus the attribute name
  (`Cellar.FV01.Temperature`), and wires attributes find-or-create — creating
  the tag (`owns_tag=True`, typed from the attribute template) or adopting an
  existing same-named tag when its type is compatible (`owns_tag=False`;
  a type conflict is an error). Wiring runs at three moments: element
  creation, tag-area assignment, and retroactively when an attribute template
  is added to an element template. Renaming or re-parenting an element resyncs
  owned tag names across its whole descendant subtree (adopted tags are left
  alone). Unwiring removes an owned tag but refuses when it has readings;
  adopted tags are only unlinked. `delete_element` and
  `delete_element_attribute_template` unwire first, so neither can silently
  destroy history. Covered by `tests/test_services_element_attributes.py`.
- `ElementAttribute` model and create-table migration (`b88bd0361d50`): an
  attribute template realized on one element and wired to a tag
  (`element_id`, `element_attribute_template_id`, `tag_id`; unique per
  element+template). `owns_tag` records whether the app auto-created the tag
  (removed with the attribute when it has no readings) or adopted an existing
  one by name (only the link is removed). `tag_id` is `ON DELETE RESTRICT`, so
  a wired tag can't be deleted out from under an attribute; deleting an
  element unwires its attributes but leaves tags and readings intact
  (verified). Wiring logic (tag-path builder, find-or-create, resync) lands
  with the service layer next.
- `clean_name_segment` validator (`services/_validation.py`) for names that
  become segments of a generated tag path: it trims, collapses internal
  whitespace runs to single spaces (so "Hot Liquor Tank" stays readable), and
  rejects the `.` tag-path separator, which would make a generated path
  ambiguous. Applied to element names and element attribute template names on
  create and update. Existing names are untouched; the rule applies going
  forward. Covered by `tests/test_services_name_segments.py`.

### Changed
- Widened `tags.name` from 45 to 255 characters (model + migration
  `cf79f49e4ee9` + service validation), so it can hold generated
  element-attribute tag paths like `Cellar.FV01.Temperature`. Data-preserving
  batch `alter_column`.
- Renamed `tag_values.timestamp` to `observed_at` (avoids the reserved word
  and follows the `_at` convention) via a data-preserving Alembic migration
  (`ba9dc47dbd8c`, batch column rename). MCP tool inputs and outputs use
  `observed_at` to match the column. New readings now default their time to
  `datetime.now(timezone.utc)` (UTC by design, not by server-clock luck).

### Added
- Admin MCP tools for element attribute templates:
  `list_element_attribute_templates`, `create_element_attribute_template`,
  `update_element_attribute_template`, `delete_element_attribute_template`
  (admin tier now 52 tools; admin owns `list_`, reference-table pattern).
  Create takes optional `lookup_id`/`measurement_unit_id`; delete is
  `confirm=true`. Completes element attribute template CRUD. Covered by
  `tests/test_mcp_config_tools_element_attribute_templates.py`.
- Service-layer CRUD for element attribute templates
  (`services/element_attribute_templates.py`): list/get/create/update/delete.
  Reuses the `Tag` type-pattern — an attribute template is lookup-typed,
  numeric, or neither (mutually exclusive), and any referenced lookup/unit
  must belong to the template's enterprise (resolved element_template → site →
  enterprise). Name unique within the element template; update covers
  name/description only.

### Changed
- Extended two more delete guards: `delete_lookup` and
  `delete_measurement_unit` now also refuse when an element attribute template
  references them. Covered by
  `tests/test_services_element_attribute_templates.py`.
- `ElementAttributeTemplate` model and create-table migration
  (`c67d381354a0`): defines an attribute on an element template (name +
  optional lookup or measurement unit, mutually exclusive like Tag). Columns:
  required `element_template_id`, nullable `lookup_id`/`measurement_unit_id`,
  `name`, `description`; unique `(element_template_id, name)`.
  `ElementTemplate` cascade-deletes its attribute templates (site teardown
  verified). Structural/type rules land with the service layer next.
- Element MCP tools with a reads-operator / writes-admin split: operator
  `list_elements` (filter by template/site/parent) and `get_element`; admin
  `create_element`, `update_element` (assign/clear `tag_area`, re-parent), and
  `delete_element` (previews child count, `confirm=true`, refused with
  children). Operator tier now 13 tools, admin 48. Completes element CRUD.
  Covered by `tests/test_mcp_element_tools.py`.
- Service-layer CRUD for elements (`services/elements.py`):
  list/get/create/update/delete. Enforces the A1 mirror rule (an element's
  parent instances its template's parent template; a top-level template's
  instances are top-level), same-site `tag_area`, and name uniqueness
  (children within their parent, roots within the template).
  `element_template_id` is immutable; update can re-parent among valid
  instances, set/clear `tag_area`, or leave them unchanged. Delete refuses
  when the element has children.

### Changed
- Extended two delete guards for the new elements: `delete_element_template`
  now also refuses when the template has element instances, and `delete_area`
  now also refuses when an element uses it as its tag area. Covered by
  `tests/test_services_elements.py`.
- `Element` model and create-table migration (`7b210831a436`): an instance of
  an `element_template` (e.g. FV01/FV02 of a Fermenter). Columns: required
  `element_template_id`, nullable `tag_area_id` (where its tags are stored),
  nullable self-FK `parent_id`, `name`, `description`; unique `(parent_id,
  name)`. `ElementTemplate` cascade-deletes its element instances, so a site
  delete unwinds the whole tree (verified). Structural rules (A1 parent mirror,
  same-site, root uniqueness) land with the service layer next.
- Admin MCP tools for element templates: `list_element_templates`,
  `create_element_template`, `update_element_template`,
  `delete_element_template` (admin tier now 43 tools; admin owns `list_` since
  the operator tier doesn't browse them). Update re-parents via `parent_id` or
  promotes to top-level via `make_top_level=true`; delete previews the child
  count, requires `confirm=true`, and is refused when children exist. Covered
  by `tests/test_mcp_config_tools_element_templates.py`.
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
