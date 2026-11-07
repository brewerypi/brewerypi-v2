"""Tests for batch properties: what a batch is, such as its brand."""

import datetime

import pytest
from sqlalchemy import select

from brewerypi import access
from brewerypi.models import (
    Element,
    EventFrameAttributeTemplate,
    EventFrameProperty,
    Tag,
    TagValue,
    User,
)
from brewerypi.services import (
    NotSelectableError,
    ValidationError,
    attributed_counts,
    create_event_frame,
    create_event_frame_attribute_template,
    create_lookup,
    create_lookup_value,
    delete_event_frame,
    delete_event_frame_attribute_template,
    delete_lookup_value,
    event_frame_property_values,
    list_event_frame_attributes,
    list_event_frames,
    property_conflicts,
    property_count,
    update_event_frame_attribute_template,
    update_event_frame_property,
    wire_event_frame_attribute,
)


def D(day, hour, minute=0):
    return datetime.datetime(2026, 7, day, hour, minute)


@pytest.fixture
def brands(flow_ctx):
    """flow_ctx plus a Brand list, required on Brew, Fermentation,
    Conditioning and Canning Run."""
    session, ids = flow_ctx
    brand = create_lookup(session, ids["ent"], "Brand")
    for name in ("Lager", "Dry Hopped Lager", "IPA", "Hazy IPA"):
        create_lookup_value(session, brand.id, name)
    for key in ("brew_t", "ferm_t", "cond_t", "can_t"):
        create_event_frame_attribute_template(
            session, ids[key], "Brand", lookup_id=brand.id,
            is_property=True, is_required=True,
        )
    ids["brand"] = brand.id
    return session, ids


def _brand(session, frame):
    return event_frame_property_values(session, frame.id).get("Brand")


# -- the problem this solves --------------------------------------------------

def test_overlapping_brews_each_keep_their_own_brand(brands):
    session, ids = brands
    first = create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew 1", started_at=D(1, 6),
        properties={"Brand": "Lager"},
    )
    second = create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew 2", started_at=D(1, 9),
        properties={"Brand": "IPA"},
    )
    assert _brand(session, first) == "Lager"
    assert _brand(session, second) == "IPA"
    lagers = list_event_frames(session, property_value="Lager")
    assert [f.name for f in lagers] == ["Brew 1"]


def test_a_property_gets_no_tag_and_cannot_be_wired(brands):
    session, ids = brands
    template = session.scalars(
        select(EventFrameAttributeTemplate).where(
            EventFrameAttributeTemplate.event_frame_template_id
            == ids["brew_t"]
        )
    ).one()
    assert list_event_frame_attributes(
        session, event_frame_attribute_template_id=template.id
    ) == []
    brand_tags = select(Tag).where(Tag.name.like("%Brand"))
    assert session.scalars(brand_tags).all() == []
    with pytest.raises(ValidationError, match="property"):
        wire_event_frame_attribute(
            session, session.get(Element, ids["bh1"]), template
        )


def test_grouping_by_brand_spans_batch_types(brands):
    session, ids = brands
    create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew 1", started_at=D(1, 6),
        properties={"Brand": "Lager"},
    )
    create_event_frame(
        session, ids["fv01"], ids["ferm_t"], "FV01 batch",
        started_at=D(1, 5), properties={"Brand": "Lager"},
    )
    create_event_frame(
        session, ids["fv02"], ids["ferm_t"], "FV02 batch",
        started_at=D(1, 5), properties={"Brand": "IPA"},
    )
    lagers = list_event_frames(session, property_value="Lager")
    names = {f.name for f in lagers}
    assert names == {"Brew 1", "FV01 batch"}


# -- starting a batch ---------------------------------------------------------

def test_a_required_property_refuses_the_start(brands):
    session, ids = brands
    with pytest.raises(ValidationError, match="A Brew needs a Brand"):
        create_event_frame(
            session, ids["bh1"], ids["brew_t"], "Brew 1", started_at=D(1, 6)
        )


def test_a_batch_type_without_the_property_has_no_brand(brands):
    session, ids = brands
    # A Centrifugation (like a CIP) defines no Brand, so it needs none.
    frame = create_event_frame(
        session, ids["cf01"], ids["cent_t"], "Spin", started_at=D(1, 6)
    )
    assert event_frame_property_values(session, frame.id) == {}


def test_unknown_names_and_options_are_refused(brands):
    session, ids = brands
    with pytest.raises(ValidationError, match="no property named 'Colour'"):
        create_event_frame(
            session, ids["bh1"], ids["brew_t"], "Brew 1",
            started_at=D(1, 6), properties={"Brand": "Lager", "Colour": "x"},
        )
    with pytest.raises(NotSelectableError):
        create_event_frame(
            session, ids["bh1"], ids["brew_t"], "Brew 1",
            started_at=D(1, 6), properties={"Brand": "Stout"},
        )


def test_a_blank_property_takes_the_start_default(flow_ctx):
    session, ids = flow_ctx
    size = create_lookup(session, ids["ent"], "Can size")
    create_lookup_value(session, size.id, "16 oz")
    twelve = create_lookup_value(session, size.id, "12 oz")
    create_event_frame_attribute_template(
        session, ids["can_t"], "Format", lookup_id=size.id,
        is_property=True, default_start_lookup_value_id=twelve.id,
    )
    run = create_event_frame(
        session, ids["cl01"], ids["can_t"], "Run 1", started_at=D(1, 6)
    )
    assert event_frame_property_values(session, run.id) == {"Format": "12 oz"}
    # and a property's default is never written as a reading
    assert session.scalars(select(TagValue)).all() == []


def test_a_blank_property_takes_the_source_batch_value(brands):
    session, ids = brands
    conditioning = create_event_frame(
        session, ids["bbt01"], ids["cond_t"], "BBT01 batch",
        started_at=D(1, 6), properties={"Brand": "Hazy IPA"},
    )
    run = create_event_frame(
        session, ids["cl01"], ids["can_t"], "Run 1", started_at=D(2, 6),
        source_event_frame_id=conditioning.id,
    )
    assert _brand(session, run) == "Hazy IPA"


def test_a_brand_can_change_where_the_beer_does(brands):
    session, ids = brands
    fermentation = create_event_frame(
        session, ids["fv01"], ids["ferm_t"], "FV01 batch",
        started_at=D(1, 6), properties={"Brand": "IPA"},
    )
    conditioning = create_event_frame(
        session, ids["bbt01"], ids["cond_t"], "BBT01 batch",
        started_at=D(2, 6), source_event_frame_id=fermentation.id,
        properties={"Brand": "Hazy IPA"},
    )
    assert _brand(session, fermentation) == "IPA"
    assert _brand(session, conditioning) == "Hazy IPA"


# -- changing a property ------------------------------------------------------

def _person(session, email):
    user = User(email=email)
    session.add(user)
    session.flush()
    return user.id


def test_a_change_keeps_who_set_it(brands):
    session, ids = brands
    sam, kim = _person(session, "sam@x.com"), _person(session, "kim@x.com")
    brew = create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew 1", started_at=D(1, 6),
        properties={"Brand": "Lager"}, started_by_id=sam,
    )
    row = update_event_frame_property(
        session, brew.id, "Brand", "IPA", updated_by_id=kim
    )
    assert (row.recorded_by_id, row.updated_by_id) == (sam, kim)
    assert _brand(session, brew) == "IPA"
    # the same value again changes nothing
    update_event_frame_property(
        session, brew.id, "Brand", "IPA", updated_by_id=sam
    )
    assert row.updated_by_id == kim
    assert attributed_counts(session, kim) == (0, 1)


def test_a_required_property_cannot_be_cleared(brands):
    session, ids = brands
    brew = create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew 1", started_at=D(1, 6),
        properties={"Brand": "Lager"},
    )
    with pytest.raises(ValidationError, match="needs a Brand"):
        update_event_frame_property(session, brew.id, "Brand", "")


def test_an_optional_property_can_be_cleared_and_set_again(flow_ctx):
    session, ids = flow_ctx
    create_event_frame_attribute_template(
        session, ids["cent_t"], "Target", is_property=True
    )
    spin = create_event_frame(
        session, ids["cf01"], ids["cent_t"], "Spin", started_at=D(1, 6)
    )
    update_event_frame_property(session, spin.id, "Target", "40")
    assert event_frame_property_values(session, spin.id) == {"Target": 40.0}
    with pytest.raises(ValidationError, match="must be a number"):
        update_event_frame_property(session, spin.id, "Target", "lots")
    cleared = update_event_frame_property(session, spin.id, "Target", None)
    assert cleared is None
    assert event_frame_property_values(session, spin.id) == {}


# -- where the beer comes from ------------------------------------------------

def _knockout(session, ids, brand, fermentation, hour):
    brew = create_event_frame(
        session, ids["bh1"], ids["brew_t"], f"{brand} brew {hour}",
        started_at=D(1, hour), properties={"Brand": brand},
    )
    cooling = create_event_frame(
        session, ids["wc1"], ids["cool_t"], "Knockout",
        started_at=D(1, hour, 30), ended_at=D(1, hour, 50),
        parent_id=brew.id, destination_event_frame_id=fermentation.id,
    )
    return brew, cooling


def test_different_brands_into_one_fermenter_warn(brands):
    session, ids = brands
    fermentation = create_event_frame(
        session, ids["fv01"], ids["ferm_t"], "FV01 batch",
        started_at=D(1, 5), properties={"Brand": "Dry Hopped Lager"},
    )
    _knockout(session, ids, "Lager", fermentation, 6)
    # Lager wort into a Dry Hopped Lager fermentation is a normal rebrand.
    assert property_conflicts(session, fermentation) == []
    _knockout(session, ids, "IPA", fermentation, 8)
    (message,) = property_conflicts(session, fermentation)
    assert "IPA brew 8 is IPA" in message
    assert "Lager brew 6 is Lager" in message


def test_yeast_from_another_brand_does_not_warn(brands):
    session, ids = brands
    donor = create_event_frame(
        session, ids["fv02"], ids["ferm_t"], "FV02 batch",
        started_at=D(1, 1), properties={"Brand": "IPA"},
    )
    fermentation = create_event_frame(
        session, ids["fv01"], ids["ferm_t"], "FV01 batch",
        started_at=D(1, 5), properties={"Brand": "Lager"},
    )
    _, cooling = _knockout(session, ids, "Lager", fermentation, 6)
    create_event_frame(
        session, ids["yd1"], ids["pitch_t"], "Pitch",
        started_at=D(1, 6, 35), ended_at=D(1, 6, 40),
        parent_id=cooling.id, source_event_frame_id=donor.id,
    )
    assert property_conflicts(session, fermentation) == []


# -- configuration and deletes ------------------------------------------------

def test_template_rules(flow_ctx):
    session, ids = flow_ctx
    with pytest.raises(ValidationError, match="only a property"):
        create_event_frame_attribute_template(
            session, ids["ferm_t"], "Gravity", is_required=True
        )
    with pytest.raises(ValidationError, match="start default at most"):
        create_event_frame_attribute_template(
            session, ids["ferm_t"], "Size", is_property=True,
            default_end_value=1.0,
        )
    gravity = create_event_frame_attribute_template(
        session, ids["ferm_t"], "Gravity"
    )
    with pytest.raises(ValidationError, match="only a property"):
        update_event_frame_attribute_template(
            session, gravity.id, is_required=True
        )


def test_deletes(brands):
    session, ids = brands
    brew = create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew 1", started_at=D(1, 6),
        properties={"Brand": "Lager"},
    )
    lager = brew.properties[0].lookup_value_id
    with pytest.raises(ValidationError, match="hold it as a property"):
        delete_lookup_value(session, lager)
    template_id = brew.properties[0].event_frame_attribute_template_id
    assert property_count(session, template_id) == 1
    delete_event_frame(session, brew.id)
    assert session.scalars(select(EventFrameProperty)).all() == []
    second = create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew 2", started_at=D(2, 6),
        properties={"Brand": "IPA"},
    )
    delete_event_frame_attribute_template(session, template_id)
    session.expire_all()
    assert event_frame_property_values(session, second.id) == {}


def test_a_property_lives_at_its_batch_site(brands):
    session, ids = brands
    brew = create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew 1", started_at=D(1, 6),
        properties={"Brand": "Lager"},
    )
    place = access.locate(session, brew.properties[0])
    assert place == access.Place(ids["ent"], ids["site"])
