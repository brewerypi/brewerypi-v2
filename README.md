# Brewery Pi

The start of the Brewery Pi project — a Python package scaffolded to
follow the standards in
[`CONVENTIONS.md`](CONVENTIONS.md): a `src/` layout, `snake_case` modules, lowercase
`kebab-case` docs, ALL-CAPS root meta files, and a SQLAlchemy `Base` preconfigured with
the constraint naming convention.

## Layout

```
brewerypi/
├── README.md                   # this file (ALL-CAPS root meta file)
├── CONVENTIONS.md              # the naming standard this repo follows
├── CHANGELOG.md
├── CONTRIBUTING.md
├── .gitignore
├── .env.example                # copy to .env (gitignored)
├── .pre-commit-config.yaml     # runs ruff (PEP 8) on every commit
├── pyproject.toml              # build config, dependencies, ruff + pytest settings
├── src/
│   └── brewerypi/                  # the importable package (snake_case)
│       ├── __init__.py
│       ├── config.py           # settings from the environment
│       ├── database.py         # SQLAlchemy Base + constraint naming convention
│       ├── models.py           # Enterprise / Site / Area (BreweryPi hierarchy)
│       └── main.py             # entry point: `python -m brewerypi.main`
├── tests/
│   ├── __init__.py
│   └── test_models.py          # pytest discovers test_*.py automatically
├── docs/
│   └── getting-started.md      # lowercase kebab-case docs
└── scripts/
    └── seed_sample_data.py     # dev helper scripts
```

## Quick start

Requires Python 3.10+.

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows PowerShell
# source .venv/bin/activate       # macOS / Linux

# 2. Install the package plus dev tools (editable install)
pip install -e ".[dev]"

# 3. Initialize the database, then run the tests
python -m brewerypi.main
pytest

# 4. (Optional) enable the pre-commit hook so ruff checks PEP 8 on every commit
pre-commit install
```

Lint manually any time with `ruff check .` (or `ruff check . --fix`).

## License

Released under the MIT License — see [LICENSE](LICENSE).
