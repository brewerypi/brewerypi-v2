# Contributing

Thanks for your interest in contributing.

## Getting set up

See the Quick start in [`README.md`](README.md) to install the project and run the
tests.

## Conventions

This project follows the naming standard in [`CONVENTIONS.md`](CONVENTIONS.md) for both
database identifiers and files. Please keep new code consistent with it.

## Linting

This project uses [ruff](https://docs.astral.sh/ruff/) to enforce PEP 8,
configured in `pyproject.toml` with the 79-character line limit. Check the
code locally with:

```bash
ruff check .          # report issues
ruff check . --fix    # auto-fix what it can
```

Enable the git hook once so ruff runs automatically on every commit:

```bash
pre-commit install
```

## Before opening a pull request

- Run `ruff check .` and the test suite (`pytest`); make sure both pass.
- Keep changes focused, and update `CHANGELOG.md` under "Unreleased" when
  behavior changes.
