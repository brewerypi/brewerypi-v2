# Getting started

This page walks through running the project locally. (Note the filename: docs inside
`docs/` use lowercase `kebab-case`, unlike the ALL-CAPS meta files at the repo root.)

## Prerequisites

- Python 3.10 or newer

## Setup

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -e ".[dev]"
```

## Running

- Initialize the database tables: `python -m brewerypi.main`
- Seed some sample data: `python scripts/seed_sample_data.py`
- Run the tests: `pytest`

## Where things live

- Application code: `src/brewerypi/`
- Models and the SQLAlchemy `Base`: `src/brewerypi/models.py` and `src/brewerypi/database.py`
- Tests: `tests/`

See [`../CONVENTIONS.md`](../CONVENTIONS.md) for the naming standard the code follows.
