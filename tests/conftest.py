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
    umbrella), fermenter, centrifuge, brite tank, canning line. Inside the
    brewhouse sits a wort cooler, and inside that a yeast doser, for the
    transfers that are steps of a batch rather than batches of their own.
    Shared by the flow-guard and lineage tests, which need the same shape.
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

        # Transfers that are steps INSIDE the batch at one end. The wort
        # cooler sits inside the brewhouse and the yeast doser inside the
        # wort cooler, so each step's template can say which end its parent
        # batch is: wort leaves the brew, yeast arrives in the wort cooling.
        nested = [
            ("wc", "Wort Cooler", "bh", "WC1", "bh1"),
            ("yd", "Yeast Doser", "wc", "YD1", "wc1"),
        ]
        for key, name, parent_key, instance, parent_instance in nested:
            template = create_element_template(
                session, site.id, name, parent_id=ids[f"{parent_key}_t"]
            )
            ids[f"{key}_t"] = template.id
            element = create_element(
                session,
                template.id,
                instance,
                tag_area_id=area.id,
                parent_id=ids[parent_instance],
            )
            ids[instance.lower()] = element.id
        cooling = create_event_frame_template(
            session, ids["wc_t"], "Wort cooling",
            step_order=1, parent_id=ids["brew_t"], parent_flow="source",
        )
        ids["cool_t"] = cooling.id
        pitch = create_event_frame_template(
            session, ids["yd_t"], "Yeast pitch",
            step_order=1, parent_id=cooling.id, parent_flow="destination",
        )
        ids["pitch_t"] = pitch.id

        yield session, ids
