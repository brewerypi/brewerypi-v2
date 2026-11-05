"""Tests for the engine every running server shares."""

from sqlalchemy import text

from brewerypi.database import create_app_engine


def test_sqlite_enforces_foreign_keys_and_waits_on_locks(tmp_path):
    engine = create_app_engine(f"sqlite:///{tmp_path / 'app.db'}")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 5000
