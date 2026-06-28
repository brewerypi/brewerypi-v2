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
- Intended future consumers: a Flask web app and an MCP server.
- `pyproject.toml` is the single source of build, dependency, and tool config.

## Commands
- Install (editable + dev tools): `pip install -e ".[dev]"`
- Run / initialize the database: `python -m brewerypi.main`
- Seed sample data: `python scripts/seed_sample_data.py`
- Tests: `pytest`
- Lint (PEP 8): `ruff check .`  (auto-fix: `ruff check . --fix`)
- Enable the commit hook: `pre-commit install`

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

## Current models (src/brewerypi/models.py)
- `Enterprise` 1—* `Site` 1—* `Area` (top of the ISA-95 equipment hierarchy).
- Each has `id`, `abbreviation` (10), `name` (45), `description` (255, nullable),
  and a parent FK with `index=True`. Name and abbreviation are unique *within
  the parent* (composite unique constraints). Relationships cascade-delete.
- Derived from upstream BreweryPi's schema.

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
- Not pushed to GitHub yet; once pushed, add a branch-protection ruleset that
  requires the CI checks to pass before merging to `main`.
- `brewerypi` is unclaimed on PyPI; reserve it if you plan to publish.
- Repo split (e.g. `brewerypi-db`) intentionally deferred until a second
  consumer exists and the schema stabilizes.
- Schema-naming decision (see above) is still open.

## In place
- MIT `LICENSE`, CI (GitHub Actions: ruff + pytest on 3.10–3.13), pre-commit
  hook, `.gitattributes` (LF normalization).
