"""Shared test fixtures.

No test should reach a real database. One that forgets to point
``mcp_server._Session`` somewhere safe otherwise falls through to
``DATABASE_URL`` and quietly uses whatever ``app.db`` sits in the working
tree: it passes for the developer who has one and fails in CI, which is
exactly how the workbook tool test broke.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi.database import Base
from brewerypi.models import Area, Enterprise, Site
from brewerypi.services import (
    create_element,
    create_element_template,
    create_event_frame_template,
)

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


@pytest.fixture
def flow_ctx():
    """A brewery wide enough for beer to move through it.

    Structure only -- no batches -- because the transfer rules are all about
    *when* frames run, so each test picks its own times. One site, with the
    equipment a batch passes through in order: brewhouse (non-exclusive, an
    umbrella), fermenter, centrifuge, brite tank, canning line. Shared by the
    flow-guard and lineage tests, which need the same shape.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E", name="Ent")
        session.add(ent)
        session.flush()
        site = Site(
            abbreviation="S", name="Site",
            enterprise_id=ent.id, timezone="UTC",
        )
        session.add(site)
        session.flush()
        area = Area(abbreviation="A", name="Area", site_id=site.id)
        session.add(area)
        session.flush()

        ids = {"ent": ent.id, "site": site.id, "area": area.id}
        equipment = [
            ("bh", "Brewhouse", False, ["BH1"]),
            ("fv", "Fermenter", True, ["FV01", "FV02"]),
            ("cf", "Centrifuge", True, ["CF01"]),
            ("bbt", "Brite Tank", True, ["BBT01"]),
            ("line", "Canning Line", True, ["CL01"]),
        ]
        for key, name, exclusive, instances in equipment:
            template = create_element_template(
                session, site.id, name, exclusive=exclusive
            )
            ids[f"{key}_t"] = template.id
            for instance in instances:
                element = create_element(
                    session, template.id, instance, tag_area_id=area.id
                )
                ids[instance.lower()] = element.id

        batches = [
            ("brew_t", "bh_t", "Brew"),
            # the knockout runs on the brewhouse, moving wort out of it
            ("knockout_t", "bh_t", "Knockout"),
            ("ferm_t", "fv_t", "Fermentation"),
            ("cent_t", "cf_t", "Centrifugation"),
            ("cond_t", "bbt_t", "Conditioning"),
            ("can_t", "line_t", "Canning Run"),
        ]
        for key, element_key, name in batches:
            template = create_event_frame_template(
                session, ids[element_key], name
            )
            ids[key] = template.id

        yield session, ids
