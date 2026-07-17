"""Tests for event frame attribute template services and guard extensions."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brewerypi.database import Base
from brewerypi.models import Enterprise, Lookup, LookupValue, Site
from brewerypi.services import (
    ConflictError,
    NotFoundError,
    ValidationError,
    create_element_template,
    create_event_frame_attribute_template,
    create_event_frame_template,
    create_measurement_unit,
    delete_event_frame_attribute_template,
    delete_lookup,
    delete_lookup_value,
    delete_measurement_unit,
    get_event_frame_attribute_template,
    list_event_frame_attribute_templates,
    update_event_frame_attribute_template,
)


@pytest.fixture
def ctx():
    """Enterprise with a Fermentation event frame template; an FV Status
    lookup (Ready to fill / Empty) and a Plato unit. Yields (session, ids)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ent = Enterprise(abbreviation="E", name="Ent")
        ent2 = Enterprise(abbreviation="E2", name="Ent Two")
        session.add_all([ent, ent2])
        session.flush()
        site = Site(
            abbreviation="S", name="Site",
            enterprise_id=ent.id, timezone="UTC",
        )
        session.add(site)
        session.flush()
        ferm_t = create_element_template(session, site.id, "Fermenter")
        ef = create_event_frame_template(session, ferm_t.id, "Fermentation")
        status = Lookup(enterprise_id=ent.id, name="FV Status")
        session.add(status)
        session.flush()
        ready = LookupValue(
            lookup_id=status.id, name="Ready to fill", is_selectable=True
        )
        empty = LookupValue(
            lookup_id=status.id, name="Empty", is_selectable=True
        )
        disabled = LookupValue(
            lookup_id=status.id, name="N/A", is_selectable=False
        )
        session.add_all([ready, empty, disabled])
        unit = create_measurement_unit(session, ent.id, "P", "Plato")
        foreign_lookup = Lookup(enterprise_id=ent2.id, name="Other")
        session.add(foreign_lookup)
        session.flush()
        yield session, {
            "ef": ef.id,
            "status": status.id,
            "ready": ready.id,
            "empty": empty.id,
            "disabled": disabled.id,
            "unit": unit.id,
            "foreign_lookup": foreign_lookup.id,
        }


def test_create_numeric_with_float_defaults(ctx):
    session, ids = ctx
    at = create_event_frame_attribute_template(
        session, ids["ef"], "Gravity",
        measurement_unit_id=ids["unit"],
        default_start_value=12.5, default_end_value=2.5,
    )
    assert at.default_start_value == 12.5
    assert at.default_end_value == 2.5


def test_create_lookup_with_value_defaults(ctx):
    session, ids = ctx
    at = create_event_frame_attribute_template(
        session, ids["ef"], "Status", lookup_id=ids["status"],
        default_start_lookup_value_id=ids["ready"],
        default_end_lookup_value_id=ids["empty"],
    )
    assert at.default_start_lookup_value_id == ids["ready"]
    assert at.default_end_lookup_value_id == ids["empty"]


def test_reject_both_types(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_event_frame_attribute_template(
            session, ids["ef"], "X",
            lookup_id=ids["status"], measurement_unit_id=ids["unit"],
        )


def test_reject_float_default_on_lookup_attr(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_event_frame_attribute_template(
            session, ids["ef"], "Status", lookup_id=ids["status"],
            default_start_value=12.5,
        )


def test_reject_lookup_default_on_numeric_attr(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_event_frame_attribute_template(
            session, ids["ef"], "Gravity",
            measurement_unit_id=ids["unit"],
            default_start_lookup_value_id=ids["ready"],
        )


def test_reject_default_from_wrong_lookup(ctx):
    session, ids = ctx
    # a value from a different lookup can't be a default here
    other_val_lookup = ids["foreign_lookup"]  # noqa: F841
    with pytest.raises((ValidationError, NotFoundError)):
        create_event_frame_attribute_template(
            session, ids["ef"], "Status", lookup_id=ids["status"],
            default_start_lookup_value_id=99999,
        )


def test_reject_non_selectable_default(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_event_frame_attribute_template(
            session, ids["ef"], "Status", lookup_id=ids["status"],
            default_start_lookup_value_id=ids["disabled"],
        )


def test_reject_foreign_lookup(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_event_frame_attribute_template(
            session, ids["ef"], "X", lookup_id=ids["foreign_lookup"]
        )


def test_duplicate_name(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(session, ids["ef"], "Gravity")
    with pytest.raises(ConflictError):
        create_event_frame_attribute_template(session, ids["ef"], "Gravity")


def test_name_rejects_dot(ctx):
    session, ids = ctx
    with pytest.raises(ValidationError):
        create_event_frame_attribute_template(session, ids["ef"], "OG.FG")


def test_update_name_and_default(ctx):
    session, ids = ctx
    at = create_event_frame_attribute_template(
        session, ids["ef"], "Gravity",
        measurement_unit_id=ids["unit"], default_start_value=12.5,
    )
    update_event_frame_attribute_template(
        session, at.id, name="OG", default_start_value=13.0
    )
    got = get_event_frame_attribute_template(session, at.id)
    assert got.name == "OG"
    assert got.default_start_value == 13.0


def test_update_clear_default(ctx):
    session, ids = ctx
    at = create_event_frame_attribute_template(
        session, ids["ef"], "Gravity",
        measurement_unit_id=ids["unit"], default_start_value=12.5,
    )
    update_event_frame_attribute_template(
        session, at.id, default_start_value=None
    )
    assert get_event_frame_attribute_template(
        session, at.id
    ).default_start_value is None


def test_update_default_type_mismatch_rejected(ctx):
    session, ids = ctx
    at = create_event_frame_attribute_template(
        session, ids["ef"], "Status", lookup_id=ids["status"],
    )
    # can't set a numeric default on a lookup-typed attribute
    with pytest.raises(ValidationError):
        update_event_frame_attribute_template(
            session, at.id, default_start_value=5.0
        )


def test_list_and_delete(ctx):
    session, ids = ctx
    a = create_event_frame_attribute_template(session, ids["ef"], "Gravity")
    create_event_frame_attribute_template(session, ids["ef"], "pH")
    assert len(
        list_event_frame_attribute_templates(
            session, event_frame_template_id=ids["ef"]
        )
    ) == 2
    delete_event_frame_attribute_template(session, a.id)
    assert len(
        list_event_frame_attribute_templates(
            session, event_frame_template_id=ids["ef"]
        )
    ) == 1


# -- guard extensions ------------------------------------------------------

def test_delete_lookup_refused_when_ef_attr_uses_it(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(
        session, ids["ef"], "Status", lookup_id=ids["status"]
    )
    with pytest.raises(ValidationError):
        delete_lookup(session, ids["status"])


def test_delete_unit_refused_when_ef_attr_uses_it(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(
        session, ids["ef"], "Gravity", measurement_unit_id=ids["unit"]
    )
    with pytest.raises(ValidationError):
        delete_measurement_unit(session, ids["unit"])


def test_delete_lookup_value_refused_when_default(ctx):
    session, ids = ctx
    create_event_frame_attribute_template(
        session, ids["ef"], "Status", lookup_id=ids["status"],
        default_start_lookup_value_id=ids["ready"],
    )
    with pytest.raises(ValidationError):
        delete_lookup_value(session, ids["ready"])
