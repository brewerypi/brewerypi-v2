"""Shared test fixtures.

No test should reach a real database. One that forgets to point
``mcp_server._Session`` somewhere safe otherwise falls through to
``DATABASE_URL`` and quietly uses whatever ``app.db`` sits in the working
tree: it passes for the developer who has one and fails in CI, which is
exactly how the workbook tool test broke.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

try:
    from brewerypi import mcp_server
except ImportError:  # the "mcp" extra is not installed
    mcp_server = None


@pytest.fixture(autouse=True)
def _no_ambient_database(tmp_path, monkeypatch):
    """Point the MCP server's session factory at an empty temp database.

    The database deliberately has no tables. A test that needs data
    overrides this with its own fixture; one that queries without meaning
    to fails loudly here, rather than succeeding against a file that
    happens to exist on one machine.
    """
    if mcp_server is None:
        return
    engine = create_engine("sqlite:///%s" % (tmp_path / "no-tables.db"))
    monkeypatch.setattr(mcp_server, "_Session", sessionmaker(engine))
