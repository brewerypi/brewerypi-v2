# Naming Conventions

A practical naming standard for a project, covering two areas: **database identifiers**
(tables, columns, constraints) and **files and directories** (source code, docs, config).
The conventions are framework-neutral, with notes for Python and SQLAlchemy where
relevant. Adopt them from day one — they cost almost nothing to follow up front and are
expensive to retrofit later.

The database sections come first; file and directory naming follows.

## Core principle

**Be consistent.** Any reasonable convention applied uniformly beats a "better"
convention applied unevenly. Everything below exists to give you one defensible default
to apply everywhere.

## Case and formatting

- **Use lowercase `snake_case` for every identifier** — tables, columns, indexes,
  constraints. Example: `order_items`, `created_at`, `customer_id`.
- **Why lowercase matters:** many databases fold unquoted identifiers to a single case
  (PostgreSQL lowercases them). Anything with capitals (`CamelCase`, `mixedCase`) then
  has to be wrapped in double quotes *everywhere* it's used. Lowercase `snake_case`
  means you never quote an identifier and your schema ports cleanly between databases
  (e.g. SQLite → PostgreSQL).
- **Allowed characters:** letters, numbers, and underscores only; start with a letter.
  No spaces, hyphens, or special characters.
- **Avoid reserved words** as names: `user`, `order`, `group`, `table`, `select`,
  `timestamp`, and similar. They force quoting and invite subtle bugs. When in doubt,
  pick a more specific name (`app_user` instead of `user`).

## Tables

- **Format:** lowercase `snake_case` (`order_items`, `audit_logs`).
- **Plural vs singular — recommended default: plural.** A table holds many rows, so
  `users`, `orders`, `readings` reads naturally. (Singular — `user`, `order` — is a
  valid alternative favored by some ORMs; if you prefer it, that's fine, just never
  mix the two.)
- **No prefixes** like `tbl_`. They're legacy noise.
- **Join (many-to-many) tables:** combine the two singular table names in alphabetical
  order, e.g. `order_product`, or use a meaningful name if the relationship is itself
  an entity (`enrollments` for students↔courses).

## Columns

- **Format:** lowercase `snake_case`, descriptive but not over-abbreviated.
- **Primary key — recommended default: `id`.** Simple and works smoothly with most ORMs.
  (The alternative is a table-qualified key like `customer_id` as the PK of `customers`,
  which makes joins read symmetrically at the cost of verbosity. Pick one and stick to
  it.)
- **Foreign keys:** name them `<referenced_table_singular>_id`. A column referencing
  `customers.id` is `customer_id`. This makes joins self-documenting.
- **Don't prefix a column with its own table name.** In `customers`, the column is
  `name` and `email`, not `customer_name` / `customer_email` — the table already
  supplies that context.
- **Timestamps and dates:** suffix `_at` for a timestamp (`created_at`, `updated_at`,
  `deleted_at`) and `_date` for a plain calendar date (`start_date`, `birth_date`).
- **Booleans:** prefix with `is_` or `has_` so the name reads as a yes/no predicate:
  `is_active`, `is_deleted`, `has_shipped`.
- **Encode units in measurement columns.** When a numeric column always holds one unit,
  put the unit in the name: `temperature_c`, `pressure_bar`, `duration_seconds`,
  `weight_kg`. This prevents an entire class of unit-confusion bugs. (If you instead
  store a generic `value` alongside a separate `unit` column, that's a reasonable
  flexible-schema alternative — just choose deliberately.)
- **Audit columns:** it's common and worthwhile to add `created_at` and `updated_at`
  to most tables from the start.

## Constraints and indexes

Give constraints predictable names rather than letting the database auto-generate them,
so migrations diff cleanly and you can reference constraints by name. A common scheme:

| Object | Pattern | Example |
|---|---|---|
| Primary key | `pk_<table>` | `pk_orders` |
| Foreign key | `fk_<table>_<column>_<ref_table>` | `fk_orders_customer_id_customers` |
| Unique | `uq_<table>_<column>` | `uq_users_email` |
| Index | `ix_<table>_<column>` | `ix_readings_recorded_at` |
| Check | `ck_<table>_<name>` | `ck_orders_total_positive` |

### SQLAlchemy: enforce it automatically

Set a naming convention on your `MetaData` once, and SQLAlchemy (and Alembic
autogenerate) will name constraints consistently for you:

```python
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

Do this before your first migration — applying it later renames existing constraints.

## Quick reference: database identifiers

| Element | Convention | Example |
|---|---|---|
| Table | lowercase, `snake_case`, plural | `order_items` |
| Column | lowercase, `snake_case` | `unit_price` |
| Primary key | `id` | `id` |
| Foreign key | `<singular_table>_id` | `customer_id` |
| Timestamp | `*_at` | `created_at` |
| Date | `*_date` | `ship_date` |
| Boolean | `is_*` / `has_*` | `is_active` |
| Measurement | `<name>_<unit>` | `weight_kg` |

## File and directory naming

### General rules

- **Default to lowercase**, and **avoid spaces and special characters**. Spaces force
  quoting and break many command-line tools and scripts; a name like `my-report.md` is
  safe everywhere, `My Report.md` is not.
- **Pick one word-separator per context:** `snake_case` for Python code, `kebab-case`
  for anything that appears in a URL or as a doc, and `PascalCase` only where a language
  or framework expects it (class files, React components).

### Source code files

- **Python:** use `snake_case.py` for modules (`process_data.py`, `claude_via_mcp.py`),
  per PEP 8. Package directories should be short, all-lowercase, and ideally without
  underscores (`api`, `models`). There's no class-per-file rule in Python — group
  related classes in one module.
- **JavaScript / TypeScript:** conventions vary by ecosystem — `kebab-case` or
  `camelCase` for modules, `PascalCase.tsx` for React components. Follow your
  framework's lead and stay consistent.
- **Match the file name to its main contents:** a module centered on `OrderService`
  belongs in `order_service.py`.

### Directories

- Lowercase and short, using `snake_case` or `kebab-case`. For Python, package
  directories must be valid import names, so use `snake_case` with **no hyphens**
  (`data_access/`, not `data-access/`).
- Conventional top-level folders: your package or `src/`, plus `tests/`, `docs/`,
  and `scripts/`.

### Documentation and meta files

- **Root-level meta files go in ALL-CAPS:** `README.md`, `LICENSE`, `CONTRIBUTING.md`,
  `CHANGELOG.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `CONVENTIONS.md`. Uppercase sorts
  them to the top of a directory listing and signals "repo-level, read first." A subset
  (`README`, `LICENSE`, `CONTRIBUTING`, `CODE_OF_CONDUCT`, `SECURITY`) is also
  auto-recognized and surfaced by GitHub.
- **Docs inside `docs/`:** lowercase `kebab-case` (`docs/getting-started.md`,
  `docs/naming-conventions.md`). The all-caps treatment is only for the root; once a doc
  lives in a subfolder, lowercase is idiomatic.

### Config and dotfiles

- Use the exact lowercase names the tools expect: `.gitignore`, `.env`, `.env.example`,
  `requirements.txt`, `pyproject.toml`, `docker-compose.yml`. `Dockerfile` is the one
  conventional exception that stays capitalized, because the tooling looks for that name.

### Cross-platform case sensitivity

This one causes real bugs: **Windows and macOS treat filenames case-insensitively, but
Linux and Git are case-sensitive.** So `Utils.py` and `utils.py` look like the same file
on your machine but are two different files on a Linux CI server or a teammate's box — a
classic "works locally, fails in CI" trap. Keeping every filename lowercase avoids it
entirely, which matters most when you develop on Windows and run CI or deploy on Linux.

### Quick reference: files and directories

| Item | Convention | Example |
|---|---|---|
| Python module | `snake_case.py` | `order_service.py` |
| Python package dir | lowercase `snake_case` | `data_access/` |
| Root meta doc | `ALL_CAPS.md` | `README.md`, `CONVENTIONS.md` |
| Doc in `docs/` | lowercase `kebab-case` | `getting-started.md` |
| Config / dotfile | exact lowercase tool name | `.gitignore`, `pyproject.toml` |

## When to deviate

Override any of these only for a deliberate reason: an existing schema or codebase you
must match, a team or company standard already in place, or a language/framework that
strongly assumes a different convention. In those cases, consistency with the
established standard wins over this one.
