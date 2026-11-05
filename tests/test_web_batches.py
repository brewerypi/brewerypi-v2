"""Tests for the batch screens, from the browser's side.

Two companies share one database, as they do on the live server, so most of
what matters here is what a signed-in person can't see.
"""

from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from brewerypi.database import Base
from brewerypi.models import Enterprise, Site
from brewerypi.services import (
    close_event_frame,
    create_element,
    create_element_template,
    create_event_frame,
    create_event_frame_template,
    create_membership,
    create_user,
)
from brewerypi.web.app import create_app
from brewerypi.web.auth import Settings, SignIn
from brewerypi.web.batches import PAGE_SIZE

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
    """My Brewery (two sites) and Other Brewery, with people and batches."""
    engine = create_engine(f"sqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as s:
        mine = Enterprise(abbreviation="M", name="My Brewery")
        theirs = Enterprise(abbreviation="O", name="Other Brewery")
        s.add_all([mine, theirs])
        s.flush()
        spr = _site(s, mine, "SPR", "Springfield")
        atl = _site(s, mine, "ATL", "Atlanta")
        sec = _site(s, theirs, "SEC", "Secret Site")

        brewhouse = create_element_template(
            s, spr.id, "Brewhouse", exclusive=False
        )
        mixer = create_element_template(
            s, spr.id, "Mash Mixer", parent_id=brewhouse.id
        )
        fermenter = create_element_template(s, spr.id, "Fermenter")
        brew = create_event_frame_template(s, brewhouse.id, "Brew")
        mashing = create_event_frame_template(
            s, mixer.id, "Mashing", step_order=1, parent_id=brew.id
        )
        fermentation = create_event_frame_template(
            s, fermenter.id, "Fermentation"
        )
        bh1 = create_element(s, brewhouse.id, "BH1")
        mm1 = create_element(s, mixer.id, "MM1", parent_id=bh1.id)
        fv01 = create_element(s, fermenter.id, "FV01")
        fv02 = create_element(s, fermenter.id, "FV02")

        sam = create_user(s, "sam@brewery.com", name="Sam Brewer")
        create_membership(s, sam.id, mine.id, "operator")
        spr_only = create_user(s, "spr@brewery.com")
        create_membership(
            s, spr_only.id, mine.id, "operator", site_ids=[spr.id]
        )
        atl_only = create_user(s, "atl@brewery.com")
        create_membership(
            s, atl_only.id, mine.id, "operator", site_ids=[atl.id]
        )

        running_brew = create_event_frame(
            s, bh1.id, brew.id, "Brew 88", started_at=_at(20),
            started_by_id=sam.id,
        )
        running_mash = create_event_frame(
            s, mm1.id, mashing.id, "Mash 88", started_at=_at(20, 13),
            parent_id=running_brew.id,
        )
        finished = create_event_frame(
            s, fv02.id, fermentation.id, "Batch 1239", started_at=_at(1),
            started_by_id=sam.id,
        )
        close_event_frame(s, finished.id, _at(10), ended_by_id=sam.id)
        running_ferment = create_event_frame(
            s, fv01.id, fermentation.id, "Batch 1240", started_at=_at(11),
        )
        knockout_type = create_event_frame_template(
            s, fermenter.id, "Transfer out"
        )
        knockout = create_event_frame(
            s, fv02.id, knockout_type.id, "Move 1239", started_at=_at(12),
            source_event_frame_id=running_ferment.id,
        )

        other_t = create_element_template(s, sec.id, "Secret Tank")
        other_batch_t = create_event_frame_template(
            s, other_t.id, "Secret Batch"
        )
        other_tank = create_element(s, other_t.id, "ST1")
        other_batch = create_event_frame(
            s, other_tank.id, other_batch_t.id, "Secret 1", started_at=_at(5)
        )
        s.commit()
        ids = {
            "spr": spr.id, "atl": atl.id, "sec": sec.id,
            "bh1": bh1.id, "fv01": fv01.id, "fv02": fv02.id,
            "fermentation": fermentation.id,
            "running_brew": running_brew.id, "running_mash": running_mash.id,
            "finished": finished.id, "running_ferment": running_ferment.id,
            "knockout": knockout.id,
            "other_tank": other_tank.id, "other_batch": other_batch.id,
        }
    return factory, ids


def _client(factory, email="sam@brewery.com"):
    """A browser signed in as ``email``."""

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
    response = client.get(
        "/callback", params={"code": "c", "state": state}
    )
    assert response.status_code == 303
    return client


@pytest.fixture
def client(world):
    return _client(world[0])


def test_every_batch_page_needs_sign_in(world):
    factory, ids = world
    anonymous = TestClient(
        create_app(_SETTINGS, factory),
        base_url=_SETTINGS.base_url,
        follow_redirects=False,
    )
    for path in (
        "/",
        f"/sites/{ids['spr']}",
        f"/equipment/{ids['fv01']}",
        f"/batches/{ids['finished']}",
    ):
        response = anonymous.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login"


def test_home_lists_my_sites_with_what_is_running(client, world):
    _, ids = world
    home = client.get("/").text
    assert "My Brewery" in home
    assert f'href="/sites/{ids["spr"]}"' in home
    # Brew 88 and Batch 1240 and Move 1239; the mash is a step inside a brew.
    assert "3 batches running" in home
    assert "0 batches running" in home  # Atlanta
    assert "Other Brewery" not in home
    assert "Secret Site" not in home


def test_site_page_nests_steps_and_separates_finished_batches(client, world):
    _, ids = world
    page = client.get(f"/sites/{ids['spr']}")
    assert page.status_code == 200
    text = page.text
    running, finished = text.split("Recently finished")
    assert "Brew 88" in running
    assert "Mash 88" in running
    assert running.index("Brew 88") < running.index("Mash 88")
    assert 'class="steps"' in running
    assert "Batch 1240" in running
    assert "Batch 1239" in finished
    assert "Batch 1239" not in running


def test_site_page_groups_equipment_and_says_what_is_busy(client, world):
    factory, ids = world
    text = client.get(f"/sites/{ids['spr']}").text
    assert "Fermenter" in text
    assert "Brewhouse" in text
    assert f'href="/equipment/{ids["fv01"]}"' in text
    assert "Busy" in text
    assert "Times are in America/Chicago." in text


def test_times_show_in_the_sites_zone(client, world):
    _, ids = world
    text = client.get(f"/batches/{ids['finished']}").text
    assert "1 Jan 2026, 6:00 AM" in text
    assert "10 Jan 2026, 6:00 AM" in text


def test_times_follow_the_users_own_zone(world):
    factory, ids = world
    with factory() as s:
        user = create_user(
            s, "london@brewery.com", timezone="Europe/London"
        )
        site = s.get(Site, ids["spr"])
        create_membership(s, user.id, site.enterprise_id, "operator")
        s.commit()
    text = _client(factory, "london@brewery.com").get(
        f"/batches/{ids['finished']}"
    ).text
    assert "1 Jan 2026, 12:00 PM" in text
    assert "Times are in Europe/London." in text


def test_equipment_page_shows_its_running_and_finished_batches(client, world):
    _, ids = world
    page = client.get(f"/equipment/{ids['fv02']}")
    assert page.status_code == 200
    running, finished = page.text.split("Finished batches")
    assert "Move 1239" in running
    assert "Batch 1239" in finished
    assert "Fermenter" in page.text


def test_batch_page_shows_who_and_when(client, world):
    _, ids = world
    text = client.get(f"/batches/{ids['finished']}").text
    assert "Batch 1239" in text
    assert "Fermentation on FV02" in text
    assert "Finished" in text
    assert text.count("by Sam Brewer") == 2
    assert "9 days" in text


def test_batch_page_shows_steps_and_parent_both_ways(client, world):
    _, ids = world
    brew = client.get(f"/batches/{ids['running_brew']}").text
    assert "Steps" in brew
    assert f'href="/batches/{ids["running_mash"]}"' in brew
    mash = client.get(f"/batches/{ids['running_mash']}").text
    assert "Part of" in mash
    assert f'href="/batches/{ids["running_brew"]}"' in mash


def test_batch_page_shows_transfers_both_ways(client, world):
    _, ids = world
    source = client.get(f"/batches/{ids['running_ferment']}").text
    assert "Transfers out" in source
    assert "Move 1239" in source
    transfer = client.get(f"/batches/{ids['knockout']}").text
    assert "Drew from" in transfer
    assert "Batch 1240" in transfer


def test_a_batch_with_no_author_says_nothing_about_one(client, world):
    _, ids = world
    text = client.get(f"/batches/{ids['running_ferment']}").text
    assert "Started" in text
    assert " by " not in text.split("<dl>")[1].split("</dl>")[0]


@pytest.mark.parametrize(
    "path",
    [
        "/sites/{sec}",
        "/equipment/{other_tank}",
        "/batches/{other_batch}",
    ],
)
def test_another_companys_rows_read_as_not_found(client, world, path):
    _, ids = world
    response = client.get(path.format(**ids))
    assert response.status_code == 404
    assert "Secret" not in response.text
    assert "ST1" not in response.text


def test_out_of_scope_and_missing_ids_look_identical(client, world):
    _, ids = world
    hidden = client.get(f"/batches/{ids['other_batch']}")
    missing = client.get("/batches/999999")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.text == missing.text


def test_a_site_restricted_user_sees_only_their_site(world):
    factory, ids = world
    client = _client(factory, "atl@brewery.com")
    home = client.get("/").text
    assert "Atlanta" in home
    assert "Springfield" not in home
    assert client.get(f"/sites/{ids['atl']}").status_code == 200
    for path in (
        f"/sites/{ids['spr']}",
        f"/equipment/{ids['fv01']}",
        f"/batches/{ids['running_brew']}",
    ):
        assert client.get(path).status_code == 404, path


def test_a_site_page_shows_nothing_from_another_site(client, world):
    _, ids = world
    text = client.get(f"/sites/{ids['atl']}").text
    assert "No batches are running at Atlanta." in text
    assert "Brew 88" not in text
    assert "Batch 1239" not in text


def test_finished_batches_page_through_older_ones(world):
    factory, ids = world
    with factory() as s:
        for day in range(1, PAGE_SIZE + 3):
            batch = create_event_frame(
                s,
                ids["fv01"],
                ids["fermentation"],
                f"Old {day:02d}",
                started_at=datetime(2025, 1, day, 1)
                if day <= 28
                else datetime(2025, 2, day - 28, 1),
                ended_at=datetime(2025, 1, day, 2)
                if day <= 28
                else datetime(2025, 2, day - 28, 2),
            )
            assert batch.id
        s.commit()
    client = _client(factory)
    first = client.get(f"/equipment/{ids['fv01']}").text
    assert f"Old {PAGE_SIZE + 2:02d}" in first
    assert "Old 01" not in first
    assert 'href="?page=2"' in first
    assert "Newer" not in first

    second = client.get(f"/equipment/{ids['fv01']}", params={"page": 2}).text
    assert "Old 01" in second
    assert f"Old {PAGE_SIZE + 2:02d}" not in second
    assert 'href="?page=1"' in second
    assert "Older" not in second


@pytest.mark.parametrize(
    "path", ["/sites/abc", "/batches/abc", "/sites/1?page=0", "/nowhere"]
)
def test_a_bad_address_gets_a_page_not_an_error_dump(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "We can" in response.text


def test_names_are_escaped(world):
    factory, ids = world
    with factory() as s:
        create_event_frame(
            s,
            ids["fv02"],
            ids["fermentation"],
            "<script>alert(1)</script>",
            started_at=datetime(2025, 3, 1),
            ended_at=datetime(2025, 3, 2),
        )
        s.commit()
    text = _client(factory).get(f"/equipment/{ids['fv02']}").text
    assert "<script>alert(1)" not in text
    assert "&lt;script&gt;alert(1)" in text


def test_the_batch_pages_offer_no_way_to_change_anything(client, world):
    _, ids = world
    for path in (
        "/",
        f"/sites/{ids['spr']}",
        f"/equipment/{ids['fv01']}",
        f"/batches/{ids['running_ferment']}",
    ):
        text = client.get(path).text
        assert text.count("<form") == 1, path
        assert 'action="/logout"' in text, path
