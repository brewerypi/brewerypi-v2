"""Tests for the reading pages: see, record, correct and delete readings.

One brewery with a fermenter's measurements and a utility meter of its own,
and another company sharing the database, as on the live server.
"""

import html
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from brewerypi.database import Base
from brewerypi.models import (
    ElementAttribute,
    Enterprise,
    EventFrameAttribute,
    LookupValue,
    Site,
    TagValue,
)
from brewerypi.services import (
    create_area,
    create_element,
    create_element_attribute_template,
    create_element_template,
    create_event_frame,
    create_event_frame_attribute_template,
    create_event_frame_template,
    create_lookup,
    create_lookup_value,
    create_measurement_unit,
    create_membership,
    create_tag,
    create_user,
)
from brewerypi.web.app import create_app
from brewerypi.web.auth import Settings, SignIn
from brewerypi.web.readings import READINGS_PAGE_SIZE

_SETTINGS = Settings(
    client_id="client_123",
    api_key="sk_test_123",
    base_url="https://app.example.com",
    session_secret="x" * 32,
)


def _at(day, hour=12):
    """A time in January 2026, UTC. Chicago is six hours behind."""
    return datetime(2026, 1, day, hour)


def _site(session, enterprise, abbreviation, name):
    site = Site(
        enterprise_id=enterprise.id,
        abbreviation=abbreviation,
        name=name,
        timezone="America/Chicago",
    )
    session.add(site)
    session.flush()
    return site


@pytest.fixture
def world(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'readings.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as s:
        mine = Enterprise(abbreviation="M", name="My Brewery")
        theirs = Enterprise(abbreviation="O", name="Other Brewery")
        s.add_all([mine, theirs])
        s.flush()
        spr = _site(s, mine, "SPR", "Springfield")
        sec = _site(s, theirs, "SEC", "Secret Site")
        cellar = create_area(s, spr.id, "CEL", "Cellar")
        utilities = create_area(s, spr.id, "UTL", "Utilities")
        celsius = create_measurement_unit(s, mine.id, "°C", "Celsius")
        status = create_lookup(s, mine.id, "FV Status")
        for name, selectable in [
            ("Fermenting", True), ("Ready", True), ("Retired", False)
        ]:
            create_lookup_value(s, status.id, name, is_selectable=selectable)

        fermenter = create_element_template(s, spr.id, "Fermenter")
        create_element_attribute_template(
            s, fermenter.id, "Temperature", measurement_unit_id=celsius.id
        )
        create_element_attribute_template(
            s, fermenter.id, "Status", lookup_id=status.id
        )
        fermentation = create_event_frame_template(
            s, fermenter.id, "Fermentation"
        )
        plato = create_measurement_unit(s, mine.id, "°P", "Plato")
        create_event_frame_attribute_template(
            s, fermentation.id, "Gravity", measurement_unit_id=plato.id
        )
        fv01 = create_element(s, fermenter.id, "FV01", tag_area_id=cellar.id)
        glycol = create_tag(
            s, utilities.id, "Glycol Supply", measurement_unit_id=celsius.id
        )

        secret_area = create_area(s, sec.id, "X", "Vault")
        secret = create_tag(s, secret_area.id, "Secret Meter")

        sam = create_user(s, "sam@brewery.com", name="Sam Brewer")
        create_membership(s, sam.id, mine.id, "operator")
        pat = create_user(s, "pat@brewery.com", name="Pat Cellar")
        create_membership(s, pat.id, mine.id, "operator")
        s.flush()

        def wired(name):
            return next(
                a.tag_id
                for a in s.scalars(select(ElementAttribute))
                if a.element_id == fv01.id
                and a.element_attribute_template.name == name
            )

        temperature = wired("Temperature")
        fv_status = wired("Status")
        reading = TagValue(
            tag_id=temperature, observed_at=_at(5), value=18.5,
            recorded_by_id=pat.id,
        )
        secret_reading = TagValue(
            tag_id=secret.id, observed_at=_at(5), value=1.0
        )
        s.add_all([reading, secret_reading])
        gravity = next(
            w.tag_id
            for w in s.scalars(select(EventFrameAttribute))
            if w.element_id == fv01.id
        )
        earlier = create_event_frame(
            s, fv01.id, fermentation.id, "Batch 1239",
            started_at=_at(1), ended_at=_at(3),
        )
        batch = create_event_frame(
            s, fv01.id, fermentation.id, "Batch 1240", started_at=_at(4)
        )
        gravity_readings = [(_at(2), 1.050), (_at(4, 13), 12.0), (_at(6), 4.5)]
        for when, value in gravity_readings:
            s.add(TagValue(tag_id=gravity, observed_at=when, value=value))
        s.commit()
        ids = {
            "spr": spr.id, "cellar": cellar.id, "utilities": utilities.id,
            "fv01": fv01.id, "temperature": temperature,
            "status": fv_status, "glycol": glycol.id,
            "reading": reading.id, "secret": secret.id,
            "secret_area": secret_area.id,
            "secret_reading": secret_reading.id,
            "sam": sam.id, "pat": pat.id,
            "gravity": gravity, "batch": batch.id, "earlier": earlier.id,
        }
    return factory, ids


def _client(factory, email="sam@brewery.com"):
    def exchange(settings, code):
        return SignIn(
            subject=f"subject-{email}",
            email=email,
            email_verified=True,
            session_id="session_1",
        )

    client = TestClient(
        create_app(_SETTINGS, factory, exchange=exchange),
        base_url=_SETTINGS.base_url,
        follow_redirects=False,
    )
    location = client.get("/login").headers["location"]
    state = parse_qs(urlsplit(location).query)["state"][0]
    client.get("/callback", params={"code": "c", "state": state})
    return client


@pytest.fixture
def client(world):
    return _client(world[0])


def _token(page: str) -> str:
    match = re.search(r'name="token" value="([^"]+)"', page)
    assert match, "the page has no confirm token"
    return match.group(1)


def _review_and_confirm(client, url, data):
    review = client.post(url, data=data)
    assert review.status_code == 200, review.text
    return client.post(
        url, data={**data, "action": "confirm", "token": _token(review.text)}
    )


def _readings(factory, tag_id) -> list[TagValue]:
    with factory() as session:
        return list(
            session.scalars(
                select(TagValue)
                .where(TagValue.tag_id == tag_id)
                .order_by(TagValue.observed_at)
            ).all()
        )


def _crumbs(page: str) -> str:
    return page.split('aria-label="Breadcrumb">')[1].split("</nav>")[0]


# -- seeing measurements ---------------------------------------------------


def test_equipment_lists_its_measurements_with_the_latest_value(
    client, world
):
    _, ids = world
    text = client.get(f"/equipment/{ids['fv01']}").text
    section = text.split("<h2>Measurements</h2>")[1]
    assert f'href="/measurements/{ids["temperature"]}"' in section
    assert "18.5 °C" in section
    assert "5 Jan 2026, 6:00 AM" in section
    assert "No readings yet" in section  # Status


def test_site_page_links_areas_that_hold_measurements_of_their_own(
    client, world
):
    _, ids = world
    text = client.get(f"/sites/{ids['spr']}").text
    section = text.split("Other measurements")[1]
    assert f'href="/areas/{ids["utilities"]}"' in section
    # The cellar only holds the fermenter's tags, reached from FV01.
    assert f'href="/areas/{ids["cellar"]}"' not in section


def test_an_area_lists_only_its_standalone_measurements(client, world):
    _, ids = world
    text = client.get(f"/areas/{ids['utilities']}").text
    assert "Glycol Supply" in text
    assert f'href="/measurements/{ids["glycol"]}"' in text
    cellar = client.get(f"/areas/{ids['cellar']}").text
    assert f'href="/measurements/{ids["temperature"]}"' not in cellar


def test_a_measurement_page_shows_recent_readings_and_who_took_them(
    client, world
):
    _, ids = world
    text = client.get(f"/measurements/{ids['temperature']}").text
    assert "<h1>Temperature</h1>" in text
    assert "On FV01 · °C" in text
    assert "18.5 °C" in text
    assert "Pat Cellar" in text
    assert f'href="/readings/{ids["reading"]}/edit"' in text
    assert f'href="/readings/{ids["reading"]}/delete"' in text


def test_a_measurement_breadcrumb_runs_through_its_equipment(client, world):
    _, ids = world
    crumbs = _crumbs(client.get(f"/measurements/{ids['temperature']}").text)
    assert f'href="/sites/{ids["spr"]}"' in crumbs
    assert f'href="/equipment/{ids["fv01"]}"' in crumbs
    standalone = _crumbs(client.get(f"/measurements/{ids['glycol']}").text)
    assert f'href="/areas/{ids["utilities"]}"' in standalone


def test_a_list_measurement_offers_only_selectable_options(client, world):
    _, ids = world
    text = client.get(f"/measurements/{ids['status']}").text
    assert '<option value="Fermenting">' in text
    assert '<option value="Ready">' in text
    assert "Retired" not in text


def test_readings_page_through_older_ones(world):
    factory, ids = world
    with factory() as s:
        for n in range(READINGS_PAGE_SIZE + 1):
            s.add(
                TagValue(
                    tag_id=ids["glycol"],
                    observed_at=_at(1) + timedelta(minutes=n),
                    value=float(n),
                )
            )
        s.commit()
    client = _client(factory)
    first = client.get(f"/measurements/{ids['glycol']}").text
    assert 'href="?page=2"' in first
    second = client.get(
        f"/measurements/{ids['glycol']}", params={"page": 2}
    ).text
    assert ">0 °C<" in second
    assert "Older" not in second


# -- recording -------------------------------------------------------------


def test_recording_reviews_then_saves_with_an_author(client, world):
    factory, ids = world
    url = f"/measurements/{ids['temperature']}"
    data = {"value": "19.25", "observed_at": "2026-01-06T07:30"}
    review = client.post(url, data=data)
    assert review.status_code == 200
    assert (
        "Record 19.25 °C for Temperature on FV01 at 6 Jan 2026, 7:30 AM?"
        in review.text
    )
    assert len(_readings(factory, ids["temperature"])) == 1
    response = client.post(
        url, data={**data, "action": "confirm", "token": _token(review.text)}
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"{url}?done=recorded"
    made = _readings(factory, ids["temperature"])[-1]
    assert made.value == 19.25
    assert made.observed_at == datetime(2026, 1, 6, 13, 30)
    assert made.recorded_by_id == ids["sam"]
    assert "Reading recorded." in client.get(
        response.headers["location"]
    ).text


def test_recording_a_list_value(client, world):
    factory, ids = world
    response = _review_and_confirm(
        client,
        f"/measurements/{ids['status']}",
        {"value": "Fermenting", "observed_at": "2026-01-06T07:30"},
    )
    assert response.status_code == 303
    (made,) = _readings(factory, ids["status"])
    with factory() as s:
        assert s.get(LookupValue, made.lookup_value_id).name == "Fermenting"


@pytest.mark.parametrize(
    "value, message",
    [
        ("", "Enter a value."),
        ("warm", "Enter a number, such as 18.5."),
        ("nan", "Enter a number, such as 18.5."),
    ],
)
def test_a_bad_number_is_refused_with_the_form_kept(
    client, world, value, message
):
    factory, ids = world
    response = client.post(
        f"/measurements/{ids['temperature']}",
        data={"value": value, "observed_at": "2026-01-06T07:30"},
    )
    assert response.status_code == 400
    assert message in response.text
    assert 'value="2026-01-06T07:30"' in response.text
    assert len(_readings(factory, ids["temperature"])) == 1


def test_an_option_not_on_the_list_is_refused(client, world):
    factory, ids = world
    response = client.post(
        f"/measurements/{ids['status']}",
        data={"value": "Retired", "observed_at": "2026-01-06T07:30"},
    )
    assert response.status_code == 400
    assert "Choose one of the options on the list." in response.text
    assert _readings(factory, ids["status"]) == []


def test_a_missing_time_is_refused(client, world):
    _, ids = world
    response = client.post(
        f"/measurements/{ids['temperature']}",
        data={"value": "19", "observed_at": ""},
    )
    assert response.status_code == 400
    assert "Enter the reading's date and time." in html.unescape(
        response.text
    )


def test_a_reading_in_the_future_is_flagged_but_allowed(client, world):
    _, ids = world
    review = client.post(
        f"/measurements/{ids['temperature']}",
        data={"value": "19", "observed_at": "2099-01-01T10:00"},
    )
    assert review.status_code == 200
    assert "That time is in the future." in review.text


def test_change_goes_back_to_the_form_with_what_was_typed(client, world):
    _, ids = world
    response = client.post(
        f"/measurements/{ids['temperature']}",
        data={
            "value": "19.25",
            "observed_at": "2026-01-06T07:30",
            "action": "change",
        },
    )
    assert response.status_code == 200
    assert 'value="19.25"' in response.text


def test_a_record_confirm_without_the_review_token_is_refused(client, world):
    factory, ids = world
    response = client.post(
        f"/measurements/{ids['temperature']}",
        data={
            "value": "19",
            "observed_at": "2026-01-06T07:30",
            "action": "confirm",
            "token": "forged",
        },
    )
    assert response.status_code == 409
    assert "check the measurement" in response.text
    assert len(_readings(factory, ids["temperature"])) == 1


def test_a_double_tap_records_only_once(client, world):
    factory, ids = world
    url = f"/measurements/{ids['temperature']}"
    data = {"value": "19", "observed_at": "2026-01-06T07:30"}
    review = client.post(url, data=data)
    confirm = {**data, "action": "confirm", "token": _token(review.text)}
    assert client.post(url, data=confirm).status_code == 303
    assert client.post(url, data=confirm).status_code == 409
    assert len(_readings(factory, ids["temperature"])) == 2


# -- correcting and deleting -----------------------------------------------


def test_correcting_shows_the_change_and_records_who(client, world):
    factory, ids = world
    url = f"/readings/{ids['reading']}/edit"
    form = client.get(url).text
    assert 'value="18.5"' in form
    assert 'value="2026-01-04T23:00"' not in form  # stays in Chicago time
    assert 'value="2026-01-05T06:00"' in form
    data = {"value": "18.0", "observed_at": "2026-01-05T06:00"}
    review = client.post(url, data=data)
    assert review.status_code == 200
    assert "Value: 18.5 °C to 18 °C" in review.text
    assert "Time:" not in review.text
    response = client.post(
        url, data={**data, "action": "confirm", "token": _token(review.text)}
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("done=reading_updated")
    (fixed,) = _readings(factory, ids["temperature"])
    assert fixed.value == 18.0
    assert fixed.recorded_by_id == ids["pat"]
    assert fixed.updated_by_id == ids["sam"]


def test_moving_a_reading_in_time(client, world):
    factory, ids = world
    response = _review_and_confirm(
        client,
        f"/readings/{ids['reading']}/edit",
        {"value": "18.5", "observed_at": "2026-01-05T08:15"},
    )
    assert response.status_code == 303
    (moved,) = _readings(factory, ids["temperature"])
    assert moved.observed_at == datetime(2026, 1, 5, 14, 15)
    assert moved.value == 18.5


def test_a_correction_that_changes_nothing_is_refused(client, world):
    _, ids = world
    response = client.post(
        f"/readings/{ids['reading']}/edit",
        data={"value": "18.50", "observed_at": "2026-01-05T06:00"},
    )
    assert response.status_code == 400
    assert "Nothing has changed." in response.text


def test_deleting_asks_first_then_removes_the_reading(client, world):
    factory, ids = world
    url = f"/readings/{ids['reading']}/delete"
    review = client.get(url)
    assert review.status_code == 200
    assert (
        "Delete the 18.5 °C reading for Temperature on FV01 at "
        "5 Jan 2026, 6:00 AM?" in review.text
    )
    assert "This can't be undone." in html.unescape(review.text)
    assert "Times are in America/Chicago." in review.text
    assert len(_readings(factory, ids["temperature"])) == 1
    response = client.post(
        url, data={"action": "confirm", "token": _token(review.text)}
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("done=reading_deleted")
    assert _readings(factory, ids["temperature"]) == []


def test_a_delete_without_the_review_token_is_refused(client, world):
    factory, ids = world
    response = client.post(
        f"/readings/{ids['reading']}/delete",
        data={"action": "confirm", "token": "forged"},
    )
    assert response.status_code == 409
    assert len(_readings(factory, ids["temperature"])) == 1


def test_reading_pages_carry_the_breadcrumb_to_the_measurement(
    client, world
):
    _, ids = world
    for path in (
        f"/readings/{ids['reading']}/edit",
        f"/readings/{ids['reading']}/delete",
    ):
        crumbs = _crumbs(client.get(path).text)
        assert f'href="/equipment/{ids["fv01"]}"' in crumbs
        assert f'href="/measurements/{ids["temperature"]}"' in crumbs


# -- what a signed-in person can't reach -----------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/measurements/{secret}",
        "/areas/{secret_area}",
        "/readings/{secret_reading}/edit",
        "/readings/{secret_reading}/delete",
    ],
)
def test_another_companys_measurements_read_as_not_found(
    client, world, path
):
    _, ids = world
    response = client.get(path.format(**ids))
    assert response.status_code == 404


def test_another_companys_readings_cant_be_written(client, world):
    factory, ids = world
    for url, data in [
        (
            f"/measurements/{ids['secret']}",
            {"value": "2", "observed_at": "2026-01-06T07:30"},
        ),
        (
            f"/readings/{ids['secret_reading']}/edit",
            {"value": "2", "observed_at": "2026-01-05T06:00"},
        ),
        (
            f"/readings/{ids['secret_reading']}/delete",
            {"action": "confirm", "token": "x"},
        ),
    ]:
        assert client.post(url, data=data).status_code == 404, url
    (untouched,) = _readings(factory, ids["secret"])
    assert untouched.value == 1.0


def test_every_reading_page_needs_sign_in(world):
    factory, ids = world
    anonymous = TestClient(
        create_app(_SETTINGS, factory),
        base_url=_SETTINGS.base_url,
        follow_redirects=False,
    )
    for path in (
        f"/areas/{ids['utilities']}",
        f"/measurements/{ids['temperature']}",
        f"/readings/{ids['reading']}/edit",
        f"/readings/{ids['reading']}/delete",
    ):
        response = anonymous.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login"


# -- seen through a batch --------------------------------------------------


def _batch_view(ids):
    return f"/measurements/{ids['gravity']}?batch={ids['batch']}"


def test_a_batch_view_shows_only_that_batchs_readings(client, world):
    _, ids = world
    text = client.get(_batch_view(ids)).text
    readings = text.split("Readings during this batch")[1]
    assert "12 °P" in readings
    assert "4.5 °P" in readings
    assert "1.05 °P" not in readings  # Batch 1239's
    assert "Readings during Batch 1240." in text
    everything = client.get(f"/measurements/{ids['gravity']}").text
    assert "1.05 °P" in everything


def test_a_batch_view_breadcrumb_runs_through_the_batch(client, world):
    _, ids = world
    crumbs = _crumbs(client.get(_batch_view(ids)).text)
    assert f'href="/batches/{ids["batch"]}"' in crumbs
    assert f'href="/equipment/{ids["fv01"]}"' in crumbs


def test_recording_for_a_batch_comes_back_to_the_batch_view(client, world):
    factory, ids = world
    url = _batch_view(ids)
    text = client.get(url).text
    assert f'action="{url}"' in html.unescape(text)
    response = _review_and_confirm(
        client, url, {"value": "4", "observed_at": "2026-01-07T07:30"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"{url}&done=recorded"
    assert _readings(factory, ids["gravity"])[-1].value == 4.0


def test_a_reading_outside_the_batch_is_flagged(client, world):
    _, ids = world
    review = client.post(
        _batch_view(ids),
        data={"value": "4", "observed_at": "2026-01-01T07:30"},
    )
    assert review.status_code == 200
    assert "That time is before this batch started." in review.text
    earlier = client.post(
        f"/measurements/{ids['gravity']}?batch={ids['earlier']}",
        data={"value": "4", "observed_at": "2026-01-06T07:30"},
    )
    assert "That time is after this batch finished." in earlier.text


def test_correcting_and_deleting_from_a_batch_view_keep_it(client, world):
    factory, ids = world
    view = client.get(_batch_view(ids)).text
    reading = max(
        r.id for r in _readings(factory, ids["gravity"]) if r.value == 4.5
    )
    edit = f"/readings/{reading}/edit?batch={ids['batch']}"
    delete = f"/readings/{reading}/delete?batch={ids['batch']}"
    assert f'href="{edit}"' in html.unescape(view)
    assert f'href="{delete}"' in html.unescape(view)
    crumbs = _crumbs(client.get(edit).text)
    assert f'href="/batches/{ids["batch"]}"' in crumbs
    fixed = _review_and_confirm(
        client, edit, {"value": "4.25", "observed_at": "2026-01-06T06:00"}
    )
    assert fixed.headers["location"] == (
        f"{_batch_view(ids)}&done=reading_updated"
    )
    review = client.get(delete)
    gone = client.post(
        delete, data={"action": "confirm", "token": _token(review.text)}
    )
    assert gone.headers["location"] == (
        f"{_batch_view(ids)}&done=reading_deleted"
    )


def test_a_batch_view_of_a_tag_the_batch_doesnt_measure_is_not_found(
    client, world
):
    _, ids = world
    for path in (
        f"/measurements/{ids['temperature']}?batch={ids['batch']}",
        f"/measurements/{ids['gravity']}?batch=99999",
        f"/readings/{ids['reading']}/edit?batch={ids['batch']}",
    ):
        assert client.get(path).status_code == 404, path
