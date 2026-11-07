"""Tests that readings and batches record who made and changed them.

Each test signs in as a person and calls the tools directly, as the sign-in
middleware would after resolving them.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from brewerypi import mcp_server
from brewerypi.database import Base
from brewerypi.identity import CurrentUser, signed_in_as
from brewerypi.models import Area, Enterprise, Site, Tag, TagValue
from brewerypi.services import (
    create_element,
    create_element_template,
    create_event_frame_attribute_template,
    create_event_frame_template,
    create_membership,
    create_user,
    get_user_by_email,
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'attribution.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(mcp_server, "_Session", factory)
    extra = {}
    with Session(engine) as session:
        ent = Enterprise(abbreviation="ATL", name="Atlas")
        site = Site(abbreviation="N", name="North", timezone="UTC")
        area = Area(abbreviation="CEL", name="Cellar")
        site.areas.append(area)
        ent.sites.append(site)
        session.add(ent)
        session.flush()
        tag = Tag(name="Temp", area_id=area.id)
        session.add(tag)
        template = create_element_template(session, site.id, "Fermenter")
        element = create_element(
            session, template.id, "FV01", tag_area_id=area.id
        )
        batch = create_event_frame_template(
            session, template.id, "Fermentation"
        )
        child_template = create_element_template(
            session, site.id, "Probe", parent_id=template.id
        )
        child = create_element(
            session,
            child_template.id,
            "P1",
            parent_id=element.id,
            tag_area_id=area.id,
        )
        child_batch = create_event_frame_template(
            session,
            child_template.id,
            "Reading run",
            parent_id=batch.id,
            step_order=1,
        )
        create_event_frame_attribute_template(
            session,
            batch.id,
            "Volume",
            default_start_value=1.0,
            default_end_value=2.0,
        )
        sam = create_user(session, "sam@brewery.com", name="Sam")
        kim = create_user(session, "kim@brewery.com")
        create_membership(session, sam.id, ent.id, "operator")
        create_membership(session, kim.id, ent.id, "operator")
        session.commit()
        for name in ("FV02", "FV03"):
            other = create_element(
                session, template.id, name, tag_area_id=area.id
            )
            extra[name] = other.id
        session.commit()
        ids = {
            **extra,
            "tag": tag.id,
            "fv": element.id,
            "ferm": batch.id,
            "probe": child.id,
            "run": child_batch.id,
        }

    def as_(email):
        with factory() as session:
            snapshot = CurrentUser.from_user(get_user_by_email(session, email))
        return signed_in_as(snapshot)

    return ids, as_, factory


SAM, KIM = "sam@brewery.com", "kim@brewery.com"


def test_a_reading_names_who_recorded_it(world):
    ids, as_, _ = world
    with as_(SAM):
        recorded = mcp_server.record_tag_value(ids["tag"], value=12.5)
    assert recorded["recorded_by"] == "Sam"
    assert recorded["updated_by"] is None
    (listed,) = mcp_server.get_tag_values(ids["tag"])["readings"]
    assert listed["recorded_by"] == "Sam"
    assert mcp_server.get_tag_value(recorded["id"])["recorded_by"] == "Sam"


def test_someone_with_no_name_is_shown_by_email(world):
    ids, as_, _ = world
    with as_(KIM):
        recorded = mcp_server.record_tag_value(ids["tag"], value=12.5)
    assert recorded["recorded_by"] == KIM


def test_a_correction_keeps_the_author_and_names_the_corrector(world):
    ids, as_, _ = world
    with as_(SAM):
        recorded = mcp_server.record_tag_value(ids["tag"], value=12.5)
    with as_(KIM):
        corrected = mcp_server.update_tag_value(recorded["id"], value=13.0)
    assert corrected["recorded_by"] == "Sam"
    assert corrected["updated_by"] == KIM


def test_changing_nothing_is_not_a_correction(world):
    ids, as_, _ = world
    with as_(SAM):
        recorded = mcp_server.record_tag_value(ids["tag"], value=12.5)
    with as_(KIM):
        same = mcp_server.update_tag_value(recorded["id"], value=12.5)
        empty = mcp_server.update_tag_value(recorded["id"])
    assert same["updated_by"] is None
    assert empty["updated_by"] is None


def test_nobody_signed_in_leaves_the_author_unknown(world):
    ids, *_ = world
    assert (
        mcp_server.record_tag_value(ids["tag"], value=12.5)["recorded_by"]
        is None
    )


def test_a_batch_names_who_started_and_closed_it(world):
    ids, as_, _ = world
    with as_(SAM):
        frame = mcp_server.create_event_frame(ids["fv"], ids["ferm"], "B1")
    assert frame["started_by"] == "Sam"
    assert frame["ended_by"] is None
    with as_(KIM):
        closed = mcp_server.close_event_frame(frame["id"])
    assert closed["started_by"] == "Sam"
    assert closed["ended_by"] == KIM
    assert closed["updated_by"] is None


def test_a_batch_entered_after_the_fact_names_one_person_for_both(world):
    ids, as_, _ = world
    with as_(SAM):
        frame = mcp_server.create_event_frame(
            ids["fv"],
            ids["ferm"],
            "B1",
            started_at="2026-01-01T00:00:00",
            ended_at="2026-01-02T00:00:00",
        )
    assert frame["started_by"] == frame["ended_by"] == "Sam"


def test_boundary_readings_belong_to_who_opened_or_closed(world):
    ids, as_, factory = world
    with as_(SAM):
        frame = mcp_server.create_event_frame(ids["fv"], ids["ferm"], "B1")
    with as_(KIM):
        mcp_server.close_event_frame(frame["id"])
    with factory() as session:
        readings = session.query(TagValue).order_by(TagValue.value).all()
        start, end = readings
        sam = get_user_by_email(session, SAM)
        kim = get_user_by_email(session, KIM)
        assert (start.value, start.recorded_by_id) == (1.0, sam.id)
        assert (end.value, end.recorded_by_id) == (2.0, kim.id)


def test_closing_a_batch_closes_nested_ones_in_that_persons_name(world):
    ids, as_, _ = world
    with as_(SAM):
        frame = mcp_server.create_event_frame(ids["fv"], ids["ferm"], "B1")
        nested = mcp_server.create_event_frame(
            ids["probe"], ids["run"], "R1", parent_id=frame["id"]
        )
    with as_(KIM):
        mcp_server.close_event_frame(frame["id"])
    assert mcp_server.get_event_frame(nested["id"])["ended_by"] == KIM


def test_reopening_clears_who_closed_it(world):
    ids, as_, _ = world
    with as_(SAM):
        frame = mcp_server.create_event_frame(ids["fv"], ids["ferm"], "B1")
        mcp_server.close_event_frame(frame["id"])
    with as_(KIM):
        reopened = mcp_server.reopen_event_frame(frame["id"])
    assert reopened["ended_by"] is None
    assert reopened["updated_by"] == KIM
    assert reopened["started_by"] == "Sam"


def test_changing_a_batch_names_who_changed_it(world):
    ids, as_, _ = world
    with as_(SAM):
        frame = mcp_server.create_event_frame(ids["fv"], ids["ferm"], "B1")
    with as_(KIM):
        unchanged = mcp_server.update_event_frame(frame["id"], name="B1")
        assert unchanged["updated_by"] is None
        renamed = mcp_server.update_event_frame(frame["id"], name="B2")
    assert renamed["updated_by"] == KIM
    assert renamed["started_by"] == "Sam"
    assert renamed["ended_by"] is None


def test_setting_an_end_through_update_records_who_ended_it(world):
    ids, as_, _ = world
    with as_(SAM):
        frame = mcp_server.create_event_frame(
            ids["fv"], ids["ferm"], "B1", started_at="2026-01-01T00:00:00"
        )
    with as_(KIM):
        ended = mcp_server.update_event_frame(
            frame["id"], ended_at="2026-01-02T00:00:00"
        )
    assert ended["ended_by"] == KIM
    with as_(SAM):
        moved = mcp_server.update_event_frame(
            frame["id"], ended_at="2026-01-03T00:00:00"
        )
    assert moved["ended_by"] == KIM  # moving the end did not close it
    assert moved["updated_by"] == "Sam"


def test_linking_a_transfer_names_who_linked_it(world):
    ids, as_, _ = world
    with as_(SAM):
        target = mcp_server.create_event_frame(
            ids["FV02"],
            ids["ferm"],
            "Target",
            started_at="2026-01-01T00:00:00",
        )
        transfer = mcp_server.create_event_frame(
            ids["FV03"],
            ids["ferm"],
            "Transfer",
            started_at="2026-01-02T00:00:00",
        )
    with as_(KIM):
        unchanged = mcp_server.link_transfer(transfer["id"])
        assert unchanged["updated_by"] is None
        linked = mcp_server.link_transfer(
            transfer["id"], destination_event_frame_id=target["id"]
        )
    assert linked["destination_event_frame_id"] == target["id"]
    assert linked["updated_by"] == KIM
    assert linked["started_by"] == "Sam"


def test_deleting_a_person_named_on_a_reading_is_refused(world):
    ids, as_, factory = world
    with as_(SAM):
        mcp_server.record_tag_value(ids["tag"], value=12.5)
    with factory() as session:
        sam_id = get_user_by_email(session, SAM).id
        kim_id = get_user_by_email(session, KIM).id
    preview = mcp_server.delete_user(sam_id)
    assert preview["confirm_required"] is False
    assert "1 reading(s)" in preview["message"]
    refused = mcp_server.delete_user(sam_id, confirm=True)
    assert "deactivate" in refused["error"]
    assert mcp_server.delete_user(kim_id)["confirm_required"] is True
    assert mcp_server.delete_user(kim_id, confirm=True) == {"deleted": kim_id}


def test_a_batch_property_names_who_set_and_changed_it(world):
    from brewerypi.models import EventFrameProperty
    from brewerypi.services import create_lookup, create_lookup_value

    ids, as_, factory = world
    with factory() as session:
        enterprise_id = session.get(Site, 1).enterprise_id
        brand = create_lookup(session, enterprise_id, "Brand")
        for name in ("Lager", "IPA"):
            create_lookup_value(session, brand.id, name)
        create_event_frame_attribute_template(
            session, ids["ferm"], "Brand", lookup_id=brand.id,
            is_property=True,
        )
        session.commit()
    with as_(SAM):
        batch = mcp_server.create_event_frame(
            ids["fv"], ids["ferm"], "B1", properties={"Brand": "Lager"}
        )
    with as_(KIM):
        mcp_server.update_event_frame_property(batch["id"], "Brand", "IPA")
    with factory() as session:
        row = session.query(EventFrameProperty).one()
        sam = get_user_by_email(session, SAM)
        kim = get_user_by_email(session, KIM)
        assert (row.recorded_by_id, row.updated_by_id) == (sam.id, kim.id)
