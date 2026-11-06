"""Tests for changing batches from the web, from the browser's side.

Mostly about what must never happen: a write without a confirmation, the same
write twice, a change with no name on it, or a change to another company's
batch. The rules themselves are tested on the services, so a few refusals here
only prove that they reach the person, before they confirm.
"""

import re
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from brewerypi.database import Base
from brewerypi.models import Enterprise, EventFrame, Site, User
from brewerypi.services import (
    create_element,
    create_element_template,
    create_event_frame,
    create_event_frame_template,
    create_membership,
    create_user,
)
from brewerypi.web.app import create_app
from brewerypi.web.auth import Settings, SignIn

_SETTINGS = Settings(
    client_id="client_123",
    api_key="sk_test_123",
    base_url="https://app.example.com",
    session_secret="x" * 32,
)


def _at(day, hour=12):
    """A time in January 2026, UTC. Chicago is six hours behind."""
    return datetime(2026, 1, day, hour)


@pytest.fixture
def world(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as s:
        mine = Enterprise(abbreviation="M", name="My Brewery")
        theirs = Enterprise(abbreviation="O", name="Other Brewery")
        s.add_all([mine, theirs])
        s.flush()
        spr = Site(
            enterprise_id=mine.id,
            abbreviation="SPR",
            name="Springfield",
            timezone="America/Chicago",
        )
        atl = Site(
            enterprise_id=mine.id,
            abbreviation="ATL",
            name="Atlanta",
            timezone="America/Chicago",
        )
        sec = Site(
            enterprise_id=theirs.id,
            abbreviation="SEC",
            name="Secret Site",
            timezone="America/Chicago",
        )
        s.add_all([spr, atl, sec])
        s.flush()

        brewhouse = create_element_template(
            s, spr.id, "Brewhouse", exclusive=False
        )
        cooler = create_element_template(
            s, spr.id, "Wort Cooler", parent_id=brewhouse.id
        )
        fermenter = create_element_template(s, spr.id, "Fermenter")
        brew = create_event_frame_template(s, brewhouse.id, "Brew")
        cooling = create_event_frame_template(
            s,
            cooler.id,
            "Wort cooling",
            step_order=1,
            parent_id=brew.id,
            parent_flow="source",
        )
        fermentation = create_event_frame_template(
            s, fermenter.id, "Fermentation"
        )
        bh1 = create_element(s, brewhouse.id, "BH1")
        wc1 = create_element(s, cooler.id, "WC1", parent_id=bh1.id)
        fv01 = create_element(s, fermenter.id, "FV01")
        fv02 = create_element(s, fermenter.id, "FV02")
        fv03 = create_element(s, fermenter.id, "FV03")

        sam = create_user(s, "sam@brewery.com", name="Sam Brewer")
        create_membership(s, sam.id, mine.id, "operator")
        pat = create_user(s, "pat@brewery.com", name="Pat Cellar")
        create_membership(s, pat.id, mine.id, "operator")
        atl_only = create_user(s, "atl@brewery.com")
        create_membership(
            s, atl_only.id, mine.id, "operator", site_ids=[atl.id]
        )
        london = create_user(
            s, "london@brewery.com", timezone="Europe/London"
        )
        create_membership(s, london.id, mine.id, "operator")

        running = create_event_frame(
            s, fv01.id, fermentation.id, "Batch 1240", started_at=_at(10),
            started_by_id=pat.id,
        )
        finished = create_event_frame(
            s, fv02.id, fermentation.id, "Batch 1239", started_at=_at(1),
            ended_at=_at(5),
        )
        other_t = create_element_template(s, sec.id, "Secret Tank")
        other_type = create_event_frame_template(s, other_t.id, "Secret Batch")
        other_tank = create_element(s, other_t.id, "ST1")
        other_batch = create_event_frame(
            s, other_tank.id, other_type.id, "Secret 1", started_at=_at(5)
        )
        s.commit()
        ids = {
            "sam": sam.id, "pat": pat.id,
            "bh1": bh1.id, "wc1": wc1.id,
            "fv01": fv01.id, "fv02": fv02.id, "fv03": fv03.id,
            "brew": brew.id, "cooling": cooling.id,
            "fermentation": fermentation.id,
            "running": running.id, "finished": finished.id,
            "other_tank": other_tank.id, "other_type": other_type.id,
            "other_batch": other_batch.id,
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


def _token(html: str) -> str:
    match = re.search(r'name="token" value="([^"]+)"', html)
    assert match, "the page has no confirm token"
    return match.group(1)


def _review_and_confirm(client, url, data):
    """Fill in a form, read the review page, and confirm it."""
    review = client.post(url, data=data)
    assert review.status_code == 200, review.text
    return client.post(
        url, data={**data, "action": "confirm", "token": _token(review.text)}
    )


def _frames(factory) -> list[EventFrame]:
    with factory() as session:
        return list(session.scalars(select(EventFrame)).all())


def _frame(factory, frame_id) -> EventFrame | None:
    with factory() as session:
        return session.get(EventFrame, frame_id)


def _start_data(ids, **overrides):
    return {
        "type_id": str(ids["fermentation"]),
        "name": "Batch 1241",
        "started_at": "2026-01-20T10:44",
        **overrides,
    }


# -- starting a batch ------------------------------------------------------


def test_a_free_vessel_offers_its_batch_types_and_a_busy_one_doesnt(
    client, world
):
    _, ids = world
    free = client.get(f"/equipment/{ids['fv03']}").text
    assert f"start?type_id={ids['fermentation']}" in free
    busy = client.get(f"/equipment/{ids['fv01']}").text
    assert "start?type_id=" not in busy


def test_a_brewhouse_always_offers_a_new_brew(client, world):
    factory, ids = world
    with factory() as s:
        create_event_frame(
            s, ids["bh1"], ids["brew"], "Brew 1", started_at=_at(20)
        )
        s.commit()
    page = client.get(f"/equipment/{ids['bh1']}").text
    assert f"start?type_id={ids['brew']}" in page


def test_the_form_offers_the_current_time_in_the_right_zone(client, world):
    _, ids = world
    page = client.get(
        f"/equipment/{ids['fv03']}/start",
        params={"type_id": ids["fermentation"]},
    )
    assert page.status_code == 200
    assert "America/Chicago" in page.text
    assert re.search(
        r'name="started_at"[^>]*value="\d{4}-\d\d-\d\dT\d\d:\d\d"',
        page.text,
    )


def test_reviewing_says_what_will_happen_and_writes_nothing(client, world):
    factory, ids = world
    before = len(_frames(factory))
    review = client.post(
        f"/equipment/{ids['fv03']}/start", data=_start_data(ids)
    )
    assert review.status_code == 200
    assert "Start Fermentation" in review.text
    assert "Batch 1241" in review.text
    assert "FV03" in review.text
    assert "20 Jan 2026, 10:44 AM" in review.text
    assert len(_frames(factory)) == before


def test_confirming_starts_the_batch_in_local_time_with_an_author(
    client, world
):
    factory, ids = world
    response = _review_and_confirm(
        client, f"/equipment/{ids['fv03']}/start", _start_data(ids)
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?done=started")
    made = next(f for f in _frames(factory) if f.name == "Batch 1241")
    assert made.started_at == datetime(2026, 1, 20, 16, 44)
    assert made.started_by_id == ids["sam"]
    assert made.element_id == ids["fv03"]
    assert "Batch started." in client.get(response.headers["location"]).text


def test_times_use_the_signed_in_users_own_zone(world):
    factory, ids = world
    client = _client(factory, "london@brewery.com")
    _review_and_confirm(
        client, f"/equipment/{ids['fv03']}/start", _start_data(ids)
    )
    made = next(f for f in _frames(factory) if f.name == "Batch 1241")
    assert made.started_at == datetime(2026, 1, 20, 10, 44)


def test_a_write_without_the_review_token_is_refused(client, world):
    factory, ids = world
    before = len(_frames(factory))
    response = client.post(
        f"/equipment/{ids['fv03']}/start",
        data=_start_data(ids, action="confirm"),
    )
    assert response.status_code == 409
    assert len(_frames(factory)) == before


def test_a_forged_token_is_refused(client, world):
    factory, ids = world
    before = len(_frames(factory))
    client.post(f"/equipment/{ids['fv03']}/start", data=_start_data(ids))
    response = client.post(
        f"/equipment/{ids['fv03']}/start",
        data=_start_data(ids, action="confirm", token="forged"),
    )
    assert response.status_code == 409
    assert len(_frames(factory)) == before


def test_a_double_tap_starts_one_batch(client, world):
    factory, ids = world
    data = _start_data(
        ids, type_id=str(ids["brew"]), name="Brew 9"
    )
    url = f"/equipment/{ids['bh1']}/start"
    review = client.post(url, data=data)
    confirm = {**data, "action": "confirm", "token": _token(review.text)}
    assert client.post(url, data=confirm).status_code == 303
    second = client.post(url, data=confirm)
    assert second.status_code == 409
    assert "already used" in second.text
    assert sum(f.name == "Brew 9" for f in _frames(factory)) == 1


def test_a_refusal_appears_before_confirming(client, world):
    factory, ids = world
    before = len(_frames(factory))
    response = client.post(
        f"/equipment/{ids['fv01']}/start", data=_start_data(ids)
    )
    assert response.status_code == 400
    assert 'class="error"' in response.text
    assert 'name="token"' not in response.text
    assert 'value="Batch 1241"' in response.text
    assert len(_frames(factory)) == before


def test_a_blank_name_and_a_blank_time_are_refused(client, world):
    _, ids = world
    url = f"/equipment/{ids['fv03']}/start"
    assert client.post(
        url, data=_start_data(ids, name="  ")
    ).status_code == 400
    blank_time = client.post(url, data=_start_data(ids, started_at=""))
    assert blank_time.status_code == 400
    assert "Enter the start date and time." in blank_time.text
    junk_time = client.post(url, data=_start_data(ids, started_at="soon"))
    assert junk_time.status_code == 400
    assert "Enter a valid start date and time." in junk_time.text


def test_a_start_in_the_future_is_flagged_but_allowed(client, world):
    _, ids = world
    review = client.post(
        f"/equipment/{ids['fv03']}/start",
        data=_start_data(ids, started_at="2099-01-01T10:00"),
    )
    assert review.status_code == 200
    assert "in the future" in review.text


def test_change_goes_back_to_the_form_with_what_was_typed(client, world):
    _, ids = world
    response = client.post(
        f"/equipment/{ids['fv03']}/start",
        data=_start_data(ids, action="change"),
    )
    assert response.status_code == 200
    assert 'value="Batch 1241"' in response.text
    assert 'value="2026-01-20T10:44"' in response.text


def test_a_step_needs_a_running_parent_and_picks_a_sole_one(client, world):
    factory, ids = world
    url = f"/equipment/{ids['wc1']}/start"
    none_yet = client.get(url, params={"type_id": ids["cooling"]}).text
    assert "Start that batch first" in none_yet
    with factory() as s:
        brew = create_event_frame(
            s, ids["bh1"], ids["brew"], "Brew 7", started_at=_at(20)
        )
        s.commit()
        brew_id = brew.id
    form = client.get(url, params={"type_id": ids["cooling"]}).text
    assert f'value="{brew_id}" selected' in form


def test_a_step_with_a_parent_flow_records_that_end_itself(client, world):
    factory, ids = world
    with factory() as s:
        brew = create_event_frame(
            s, ids["bh1"], ids["brew"], "Brew 7", started_at=_at(20)
        )
        s.commit()
        brew_id = brew.id
    url = f"/equipment/{ids['wc1']}/start"
    form = client.get(url, params={"type_id": ids["cooling"]}).text
    assert 'name="source_id"' not in form
    assert "draws from the batch" in form
    response = _review_and_confirm(
        client,
        url,
        {
            "type_id": str(ids["cooling"]),
            "name": "Knockout 7",
            "started_at": "2026-01-20T10:00",
            "parent_id": str(brew_id),
            "destination_id": str(ids["running"]),
        },
    )
    assert response.status_code == 303
    made = next(f for f in _frames(factory) if f.name == "Knockout 7")
    assert made.parent_id == brew_id
    assert made.source_event_frame_id == brew_id
    assert made.destination_event_frame_id == ids["running"]


# -- closing and reopening -------------------------------------------------


def test_closing_needs_a_review_and_names_who_closed_it(client, world):
    factory, ids = world
    url = f"/batches/{ids['running']}/close"
    data = {"ended_at": "2026-01-20T10:00"}
    review = client.post(url, data=data)
    assert review.status_code == 200
    assert "Close Fermentation" in review.text
    assert _frame(factory, ids["running"]).ended_at is None
    response = client.post(
        url, data={**data, "action": "confirm", "token": _token(review.text)}
    )
    assert response.status_code == 303
    closed = _frame(factory, ids["running"])
    assert closed.ended_at == datetime(2026, 1, 20, 16, 0)
    assert closed.ended_by_id == ids["sam"]
    assert closed.started_by_id == ids["pat"]


def test_closing_warns_about_the_steps_inside(client, world):
    factory, ids = world
    with factory() as s:
        brew = create_event_frame(
            s, ids["bh1"], ids["brew"], "Brew 7", started_at=_at(20)
        )
        create_event_frame(
            s, ids["wc1"], ids["cooling"], "Knockout 7",
            started_at=_at(20, 13), parent_id=brew.id,
        )
        s.commit()
        brew_id = brew.id
    review = client.post(
        f"/batches/{brew_id}/close", data={"ended_at": "2026-01-21T10:00"}
    )
    assert "also closes 1 running step inside" in review.text


def test_closing_before_it_started_is_refused_on_the_form(client, world):
    factory, ids = world
    response = client.post(
        f"/batches/{ids['running']}/close",
        data={"ended_at": "2026-01-02T10:00"},
    )
    assert response.status_code == 400
    assert 'name="token"' not in response.text
    assert _frame(factory, ids["running"]).ended_at is None


def test_a_finished_batch_has_no_close_form(client, world):
    _, ids = world
    response = client.get(f"/batches/{ids['finished']}/close")
    assert response.status_code == 303


def test_reopening_clears_the_end_and_names_who_did_it(client, world):
    factory, ids = world
    url = f"/batches/{ids['finished']}/reopen"
    review = client.get(url)
    assert review.status_code == 200
    assert "Reopen Fermentation" in review.text
    assert _frame(factory, ids["finished"]).ended_at is not None
    response = client.post(url, data={"token": _token(review.text)})
    assert response.status_code == 303
    reopened = _frame(factory, ids["finished"])
    assert reopened.ended_at is None
    assert reopened.updated_by_id == ids["sam"]


def test_reopening_into_a_busy_vessel_is_refused_before_confirming(
    client, world
):
    factory, ids = world
    with factory() as s:
        create_event_frame(
            s, ids["fv02"], ids["fermentation"], "Now", started_at=_at(20)
        )
        s.commit()
    response = client.get(f"/batches/{ids['finished']}/reopen")
    assert response.status_code == 409
    assert 'name="token"' not in response.text
    assert _frame(factory, ids["finished"]).ended_at is not None


def test_reopen_needs_the_token(client, world):
    factory, ids = world
    response = client.post(
        f"/batches/{ids['finished']}/reopen", data={"token": "forged"}
    )
    assert response.status_code == 409
    assert _frame(factory, ids["finished"]).ended_at is not None


# -- editing ---------------------------------------------------------------


def test_editing_shows_the_changes_then_saves_them_with_an_author(
    client, world
):
    factory, ids = world
    url = f"/batches/{ids['finished']}/edit"
    data = {
        "name": "Batch 1239B",
        "started_at": "2026-01-01T05:00",
        "ended_at": "2026-01-05T06:00",
    }
    review = client.post(url, data=data)
    assert review.status_code == 200
    assert "Batch 1239B" in review.text
    assert "Start: 1 Jan 2026, 6:00 AM to 1 Jan 2026, 5:00 AM" in review.text
    assert "stay where they are" in review.text
    assert _frame(factory, ids["finished"]).name == "Batch 1239"
    response = client.post(
        url, data={**data, "action": "confirm", "token": _token(review.text)}
    )
    assert response.status_code == 303
    edited = _frame(factory, ids["finished"])
    assert edited.name == "Batch 1239B"
    assert edited.started_at == datetime(2026, 1, 1, 11, 0)
    assert edited.ended_at == _at(5)
    assert edited.updated_by_id == ids["sam"]


def test_an_edit_that_changes_nothing_is_refused(client, world):
    _, ids = world
    response = client.post(
        f"/batches/{ids['finished']}/edit",
        data={
            "name": "Batch 1239",
            "started_at": "2026-01-01T06:00",
            "ended_at": "2026-01-05T06:00",
        },
    )
    assert response.status_code == 400
    assert "Nothing has changed" in response.text


def test_an_edit_moving_a_time_into_the_future_is_flagged(client, world):
    factory, ids = world
    with factory() as s:
        batch = create_event_frame(
            s,
            ids["fv03"],
            ids["fermentation"],
            "Later",
            started_at=datetime(2026, 1, 15, 12, 0),
        )
        s.commit()
        batch_id = batch.id
    url = f"/batches/{batch_id}/edit"
    review = client.post(
        url, data={"name": "Later", "started_at": "2099-01-01T10:00"}
    )
    assert review.status_code == 200
    assert "The new start time is in the future." in review.text
    rename = client.post(
        url, data={"name": "Later 2", "started_at": "2026-01-15T06:00"}
    )
    assert rename.status_code == 200
    assert "in the future" not in rename.text


def test_renaming_leaves_seconds_on_the_stored_times_alone(client, world):
    factory, ids = world
    exact = datetime(2026, 1, 15, 18, 30, 45)
    with factory() as s:
        batch = create_event_frame(
            s, ids["fv03"], ids["fermentation"], "Exact", started_at=exact
        )
        s.commit()
        batch_id = batch.id
    response = _review_and_confirm(
        client,
        f"/batches/{batch_id}/edit",
        {"name": "Exact 2", "started_at": "2026-01-15T12:30", "ended_at": ""},
    )
    assert response.status_code == 303
    edited = _frame(factory, batch_id)
    assert edited.name == "Exact 2"
    assert edited.started_at == exact


def test_a_running_batch_cant_be_ended_from_the_edit_page(client, world):
    factory, ids = world
    form = client.get(f"/batches/{ids['running']}/edit").text
    assert 'name="ended_at"' not in form
    _review_and_confirm(
        client,
        f"/batches/{ids['running']}/edit",
        {
            "name": "Renamed",
            "started_at": "2026-01-10T06:00",
            "ended_at": "2026-01-20T10:00",
        },
    )
    assert _frame(factory, ids["running"]).ended_at is None


# -- transfers -------------------------------------------------------------


def test_linking_a_transfer_late_names_who_did_it(client, world):
    factory, ids = world
    with factory() as s:
        move = create_event_frame(
            s, ids["fv03"], ids["fermentation"], "Move", started_at=_at(12)
        )
        s.commit()
        move_id = move.id
    url = f"/batches/{move_id}/transfer"
    form = client.get(url).text
    assert f'<option value="{ids["running"]}"' in form
    assert f'<option value="{ids["finished"]}"' not in form
    response = _review_and_confirm(
        client, url, {"source_id": str(ids["running"]), "destination_id": ""}
    )
    assert response.status_code == 303
    linked = _frame(factory, move_id)
    assert linked.source_event_frame_id == ids["running"]
    assert linked.updated_by_id == ids["sam"]


def test_clearing_a_transfer_end(client, world):
    factory, ids = world
    with factory() as s:
        move = create_event_frame(
            s, ids["fv03"], ids["fermentation"], "Move", started_at=_at(12),
            source_event_frame_id=ids["running"],
        )
        s.commit()
        move_id = move.id
    url = f"/batches/{move_id}/transfer"
    assert f'value="{ids["running"]}" selected' in client.get(url).text
    _review_and_confirm(client, url, {"source_id": "", "destination_id": ""})
    assert _frame(factory, move_id).source_event_frame_id is None


def test_a_transfer_end_that_doesnt_fit_is_refused_on_the_form(client, world):
    factory, ids = world
    with factory() as s:
        move = create_event_frame(
            s, ids["fv03"], ids["fermentation"], "Move", started_at=_at(12)
        )
        s.commit()
        move_id = move.id
    response = client.post(
        f"/batches/{move_id}/transfer",
        data={"source_id": str(ids["finished"]), "destination_id": ""},
    )
    assert response.status_code == 400
    assert 'name="token"' not in response.text
    assert _frame(factory, move_id).source_event_frame_id is None


def test_an_implied_transfer_end_stays_fixed(client, world):
    factory, ids = world
    with factory() as s:
        brew = create_event_frame(
            s, ids["bh1"], ids["brew"], "Brew 7", started_at=_at(20)
        )
        cool = create_event_frame(
            s, ids["wc1"], ids["cooling"], "Knockout 7",
            started_at=_at(20, 13), parent_id=brew.id,
        )
        s.commit()
        brew_id, cool_id = brew.id, cool.id
    url = f"/batches/{cool_id}/transfer"
    assert 'name="source_id"' not in client.get(url).text
    _review_and_confirm(
        client, url, {"destination_id": str(ids["running"])}
    )
    linked = _frame(factory, cool_id)
    assert linked.source_event_frame_id == brew_id
    assert linked.destination_event_frame_id == ids["running"]


# -- deleting --------------------------------------------------------------


def test_deleting_previews_then_goes_back_to_the_equipment(client, world):
    factory, ids = world
    url = f"/batches/{ids['finished']}/delete"
    review = client.get(url)
    assert review.status_code == 200
    assert "Delete Fermentation" in review.text
    assert "can&#39;t be undone" in review.text
    assert "Readings and measurements are kept" in review.text
    assert _frame(factory, ids["finished"]) is not None
    response = client.post(url, data={"token": _token(review.text)})
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/equipment/{ids['fv02']}?done=deleted"
    )
    assert _frame(factory, ids["finished"]) is None


def test_deleting_says_how_many_steps_go_with_it(client, world):
    factory, ids = world
    with factory() as s:
        brew = create_event_frame(
            s, ids["bh1"], ids["brew"], "Brew 7", started_at=_at(20)
        )
        create_event_frame(
            s, ids["wc1"], ids["cooling"], "Knockout 7",
            started_at=_at(20, 13), parent_id=brew.id,
        )
        s.commit()
        brew_id = brew.id
    review = client.get(f"/batches/{brew_id}/delete")
    assert "also deletes 1 step inside" in review.text


def test_a_batch_a_transfer_points_at_cant_be_deleted(client, world):
    factory, ids = world
    with factory() as s:
        move = create_event_frame(
            s, ids["fv03"], ids["fermentation"], "Move", started_at=_at(12),
            source_event_frame_id=ids["running"],
        )
        s.commit()
        move_id = move.id
    response = client.get(f"/batches/{ids['running']}/delete")
    assert response.status_code == 409
    assert 'name="token"' not in response.text
    assert f'href="/batches/{move_id}"' in response.text
    assert _frame(factory, ids["running"]) is not None


def test_delete_needs_the_token(client, world):
    factory, ids = world
    response = client.post(
        f"/batches/{ids['finished']}/delete", data={"token": "forged"}
    )
    assert response.status_code == 409
    assert _frame(factory, ids["finished"]) is not None


def test_a_token_from_one_review_is_spent_by_any_write(client, world):
    factory, ids = world
    token = _token(client.get(f"/batches/{ids['finished']}/delete").text)
    other = client.post(
        f"/batches/{ids['running']}/close",
        data={
            "ended_at": "2026-01-20T10:00",
            "action": "confirm",
            "token": token,
        },
    )
    assert other.status_code == 303
    again = client.post(
        f"/batches/{ids['finished']}/delete", data={"token": token}
    )
    assert again.status_code == 409
    assert _frame(factory, ids["finished"]) is not None


# -- who may do it ---------------------------------------------------------


@pytest.mark.parametrize(
    "method, path, data",
    [
        ("get", "/equipment/{other_tank}/start?type_id={other_type}", None),
        ("post", "/equipment/{other_tank}/start", {"type_id": "{other_type}"}),
        ("get", "/batches/{other_batch}/close", None),
        ("post", "/batches/{other_batch}/close", {}),
        ("get", "/batches/{other_batch}/reopen", None),
        ("post", "/batches/{other_batch}/reopen", {}),
        ("get", "/batches/{other_batch}/edit", None),
        ("post", "/batches/{other_batch}/edit", {}),
        ("get", "/batches/{other_batch}/transfer", None),
        ("post", "/batches/{other_batch}/transfer", {}),
        ("get", "/batches/{other_batch}/delete", None),
        ("post", "/batches/{other_batch}/delete", {}),
    ],
)
def test_another_companys_batches_read_as_not_found(
    client, world, method, path, data
):
    factory, ids = world
    url = path.format(**ids)
    if data is None:
        response = client.get(url)
    else:
        filled = {k: v.format(**ids) for k, v in data.items()}
        response = client.post(url, data=filled)
    assert response.status_code == 404
    assert "Secret" not in response.text
    assert _frame(factory, ids["other_batch"]).name == "Secret 1"


def test_another_companys_type_cant_be_started_on_my_vessel(client, world):
    factory, ids = world
    before = len(_frames(factory))
    response = client.post(
        f"/equipment/{ids['fv03']}/start",
        data=_start_data(ids, type_id=str(ids["other_type"])),
    )
    assert response.status_code == 404
    assert len(_frames(factory)) == before


@pytest.mark.parametrize("field", ["parent_id", "source_id", "destination_id"])
def test_another_companys_batch_cant_be_linked_when_starting(
    client, world, field
):
    factory, ids = world
    before = len(_frames(factory))
    response = client.post(
        f"/equipment/{ids['fv03']}/start",
        data=_start_data(ids, **{field: str(ids["other_batch"])}),
    )
    assert response.status_code == 404
    assert len(_frames(factory)) == before


def test_another_companys_batch_cant_be_linked_later(client, world):
    factory, ids = world
    response = client.post(
        f"/batches/{ids['running']}/transfer",
        data={"source_id": str(ids["other_batch"]), "destination_id": ""},
    )
    assert response.status_code == 404
    assert _frame(factory, ids["running"]).source_event_frame_id is None


def test_a_site_restricted_user_cant_change_another_site(world):
    factory, ids = world
    client = _client(factory, "atl@brewery.com")
    assert client.get(f"/batches/{ids['running']}/close").status_code == 404
    response = client.post(
        f"/batches/{ids['running']}/close",
        data={"ended_at": "2026-01-20T10:00"},
    )
    assert response.status_code == 404
    assert _frame(factory, ids["running"]).ended_at is None


def test_signed_out_writes_go_to_sign_in_and_change_nothing(world):
    factory, ids = world
    anonymous = TestClient(
        create_app(_SETTINGS, factory),
        base_url=_SETTINGS.base_url,
        follow_redirects=False,
    )
    response = anonymous.post(
        f"/batches/{ids['finished']}/delete", data={"token": "x"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert _frame(factory, ids["finished"]) is not None


def test_someone_deactivated_mid_review_cant_confirm(client, world):
    factory, ids = world
    url = f"/batches/{ids['finished']}/delete"
    token = _token(client.get(url).text)
    with factory() as s:
        s.scalar(
            select(User).where(User.email == "sam@brewery.com")
        ).is_active = False
        s.commit()
    response = client.post(url, data={"token": token})
    assert response.status_code == 403
    assert _frame(factory, ids["finished"]) is not None


def test_typed_text_is_escaped_on_review_pages(client, world):
    _, ids = world
    review = client.post(
        f"/equipment/{ids['fv03']}/start",
        data=_start_data(ids, name="<script>x</script>"),
    )
    assert "<script>x</script>" not in review.text


def test_the_batch_page_offers_what_fits_its_state(client, world):
    _, ids = world
    running = client.get(f"/batches/{ids['running']}").text
    assert f"/batches/{ids['running']}/close" in running
    assert "/reopen" not in running
    finished = client.get(f"/batches/{ids['finished']}").text
    assert f"/batches/{ids['finished']}/reopen" in finished
    assert "/close" not in finished


def test_a_crafted_notice_cant_put_words_on_the_page(client, world):
    _, ids = world
    page = client.get(
        f"/batches/{ids['running']}", params={"done": "Send money"}
    ).text
    assert "Send money" not in page
    assert 'class="notice"' not in page


def test_closing_a_batch_a_transfer_runs_through_is_refused(client, world):
    factory, ids = world
    with factory() as s:
        create_event_frame(
            s, ids["fv03"], ids["fermentation"], "Move", started_at=_at(12),
            source_event_frame_id=ids["running"],
        )
        s.commit()
    response = client.post(
        f"/batches/{ids['running']}/close",
        data={"ended_at": "2026-01-20T10:00"},
    )
    assert response.status_code == 400
    assert 'class="error"' in response.text
    assert _frame(factory, ids["running"]).ended_at is None
