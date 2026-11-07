"""Tests for the batch screens, from the browser's side.

Two companies share one database, as they do on the live server, so most of
what matters here is what a signed-in person can't see.
"""

import html
import re
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from brewerypi.database import Base
from brewerypi.models import (
    Element,
    ElementAttribute,
    Enterprise,
    EventFrameAttribute,
    LookupValue,
    Site,
    Tag,
    TagValue,
)
from brewerypi.services import (
    close_event_frame,
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
    create_user,
)
from brewerypi.web.app import create_app
from brewerypi.web.auth import Settings, SignIn
from brewerypi.web.batches import PAGE_SIZE, _busy

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
            "spr": spr.id, "atl": atl.id, "sec": sec.id, "sam": sam.id,
            "bh1": bh1.id, "mm1": mm1.id, "fv01": fv01.id, "fv02": fv02.id,
            "fermentation": fermentation.id, "brew": brew.id,
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


def test_running_batches_filter_by_type(client, world):
    _, ids = world
    url = f"/sites/{ids['spr']}"
    everything = client.get(url).text
    assert 'aria-label="Show running batch type"' in everything
    assert 'All <span class="pill">3</span>' in everything
    assert f'href="{url}?running=Transfer+out"' in everything
    running = client.get(url, params={"running": "Fermentation"}).text
    shown, finished = running.split("Recently finished")
    assert "Batch 1240" in shown
    assert "Brew 88" not in shown
    assert "Move 1239" not in shown
    # Only the running list is filtered.
    assert "Batch 1239" in finished


def test_filtering_keeps_steps_inside_their_batch(client, world):
    _, ids = world
    text = client.get(
        f"/sites/{ids['spr']}", params={"running": "Brew"}
    ).text
    shown = text.split("Recently finished")[0]
    assert "Brew 88" in shown
    assert "Mash 88" in shown
    assert "Batch 1240" not in shown
    # A step isn't offered as a filter of its own.
    assert "=Mashing" not in text


def test_a_type_with_nothing_running_says_so(client, world):
    _, ids = world
    text = client.get(
        f"/sites/{ids['spr']}", params={"running": "Lagering"}
    ).text
    assert "No Lagering batches are running at Springfield." in text
    assert f'href="/sites/{ids["spr"]}"' in text


def test_one_type_running_needs_no_filter(client, world):
    factory, ids = world
    with factory() as s:
        tank_t = create_element_template(s, ids["atl"], "Tank")
        batch_t = create_event_frame_template(s, tank_t.id, "Conditioning")
        tank = create_element(s, tank_t.id, "T1")
        create_event_frame(
            s, tank.id, batch_t.id, "Only one", started_at=_at(3)
        )
        s.commit()
    text = client.get(f"/sites/{ids['atl']}").text
    assert "Only one" in text
    assert 'aria-label="Show running batch type"' not in text


def _finish_a_brew(factory, ids):
    """A second finished type at Springfield, so its filter has a choice."""
    with factory() as s:
        create_event_frame(
            s, ids["bh1"], ids["brew"], "Brew 80",
            started_at=_at(2), ended_at=_at(2, 18),
        )
        s.commit()


def test_finished_batches_filter_by_type(world):
    factory, ids = world
    _finish_a_brew(factory, ids)
    client = _client(factory)
    url = f"/sites/{ids['spr']}"
    everything = client.get(url).text
    finished = everything.split("Recently finished")[1]
    assert 'aria-label="Show finished batch type"' in finished
    assert 'All <span class="pill">2</span>' in finished
    assert f'href="{url}?finished=Brew"' in finished
    text = client.get(url, params={"finished": "Brew"}).text
    running, finished = text.split("Recently finished")
    assert "Brew 80" in finished
    assert "Batch 1239" not in finished
    # The running list is left alone.
    assert "Batch 1240" in running


def test_each_list_keeps_the_other_lists_choice(world):
    factory, ids = world
    _finish_a_brew(factory, ids)
    url = f"/sites/{ids['spr']}"
    text = _client(factory).get(
        url, params={"running": "Brew", "finished": "Fermentation"}
    ).text
    running, finished = text.split("Recently finished")
    assert f'href="{url}?finished=Fermentation&amp;running=Fermentation"' \
        in running
    assert f'href="{url}?running=Brew&amp;finished=Brew"' in finished
    # Each row's All clears only its own list.
    assert f'href="{url}?finished=Fermentation"' in running
    assert f'href="{url}?running=Brew"' in finished


def test_a_finished_type_with_nothing_finished_says_so(client, world):
    _, ids = world
    text = client.get(
        f"/sites/{ids['spr']}", params={"finished": "Lagering"}
    ).text
    assert "No finished Lagering batches to show." in text


def test_paging_finished_batches_keeps_both_filters(world):
    factory, ids = world
    with factory() as s:
        for day in range(1, PAGE_SIZE + 2):
            create_event_frame(
                s, ids["fv02"], ids["fermentation"], f"Old {day:02d}",
                started_at=datetime(2025, 1, day, 1),
                ended_at=datetime(2025, 1, day, 2),
            )
        s.commit()
    client = _client(factory)
    url = f"/sites/{ids['spr']}"
    text = client.get(
        url, params={"running": "Brew", "finished": "Fermentation"}
    ).text
    assert 'href="?running=Brew&amp;finished=Fermentation&amp;page=2"' in text
    second = client.get(
        url,
        params={"running": "Brew", "finished": "Fermentation", "page": 2},
    ).text
    assert "Old 01" in second
    # Choosing a running type keeps the page you're on; a finished type
    # starts over.
    running, finished = second.split("Recently finished")
    assert "page=2" in running
    assert "page=2" not in finished.split("</nav>")[0]


def test_site_page_lists_only_top_level_equipment(client, world):
    _, ids = world
    text = client.get(f"/sites/{ids['spr']}").text
    assert f'href="/equipment/{ids["bh1"]}"' in text
    assert f'href="/equipment/{ids["mm1"]}"' not in text
    assert "Mash Mixer" not in text.split("<h2>Equipment</h2>")[1]


def test_equipment_page_lists_what_is_inside_it(client, world):
    _, ids = world
    text = client.get(f"/equipment/{ids['bh1']}").text
    inside = text.split("Inside this equipment")[1]
    assert f'href="/equipment/{ids["mm1"]}"' in inside
    assert "Busy" in inside
    # Equipment with nothing inside has no such section.
    assert "Inside this equipment" not in client.get(
        f"/equipment/{ids['fv01']}"
    ).text


def test_equipment_breadcrumb_climbs_back_up(client, world):
    _, ids = world
    crumbs = client.get(f"/equipment/{ids['mm1']}").text.split("</nav>")[0]
    assert f'href="/sites/{ids["spr"]}"' in crumbs
    assert f'href="/equipment/{ids["bh1"]}"' in crumbs


def test_equipment_is_busy_when_anything_inside_it_is():
    def element(id, parent_id=None):
        return SimpleNamespace(id=id, parent_id=parent_id)

    brewhouse, mixer, kettle, tank = (
        element(1), element(2, 1), element(3, 2), element(4)
    )
    busy = _busy([brewhouse, mixer, kettle, tank], {3})
    assert busy == {1, 2, 3}


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
    assert "<h2>Source</h2>" in transfer
    assert "Batch 1240" in transfer


def test_batch_breadcrumb_runs_through_the_equipment_it_sits_in(
    client, world
):
    _, ids = world
    text = client.get(f"/batches/{ids['running_mash']}").text
    crumbs = text.split('aria-label="Breadcrumb">')[1].split("</nav>")[0]
    assert crumbs.index(f'href="/sites/{ids["spr"]}"') < crumbs.index(
        f'href="/equipment/{ids["bh1"]}"'
    ) < crumbs.index(f'href="/equipment/{ids["mm1"]}"')


def _measured_batch(factory, ids, close=False):
    """A fermentation on a new FV09 whose Gravity is measured, with readings.

    The fixture's FV01 has no area for its tags, so its Gravity isn't set
    up; FV09 has one.
    """
    with factory() as s:
        site = s.get(Site, ids["spr"])
        cellar = create_area(s, site.id, "CEL", "Cellar")
        plato = create_measurement_unit(s, site.enterprise_id, "°P", "Plato")
        create_event_frame_attribute_template(
            s, ids["fermentation"], "Gravity", measurement_unit_id=plato.id
        )
        fermenter_t = s.get(Element, ids["fv01"]).element_template_id
        fv09 = create_element(s, fermenter_t, "FV09", tag_area_id=cellar.id)
        gravity = next(
            w.tag_id
            for w in s.scalars(select(EventFrameAttribute))
            if w.element_id == fv09.id
        )
        batch = create_event_frame(
            s, fv09.id, ids["fermentation"], "Batch 2001",
            started_at=_at(20),
        )
        for when, value in [(_at(19), 1.0), (_at(20, 13), 12.0),
                            (_at(21), 4.5)]:
            s.add(TagValue(tag_id=gravity, observed_at=when, value=value))
        if close:
            close_event_frame(s, batch.id, _at(22))
        s.commit()
        return batch.id, gravity


def test_a_batch_page_sums_up_its_measurements(client, world):
    factory, ids = world
    batch, gravity = _measured_batch(factory, ids)
    text = client.get(f"/batches/{batch}").text
    section = text.split("<h2>Measurements</h2>")[1].split("</section>")[0]
    assert "Gravity" in section
    assert "4.5 °P" in section
    assert "Started at 12 °P" in section
    # The reading before the batch started isn't counted.
    assert "2 readings" in section
    view = f"/measurements/{gravity}?batch={batch}"
    assert f'href="{view}"' in section
    # Readings go in the box on this page, not one page each.
    assert f'name="value_{gravity}"' in section
    assert f'action="/batches/{batch}/readings"' in section


def test_a_finished_batch_has_no_boxes_to_fill_in(client, world):
    factory, ids = world
    batch, gravity = _measured_batch(factory, ids, close=True)
    section = client.get(f"/batches/{batch}").text.split(
        "<h2>Measurements</h2>"
    )[1]
    assert 'name="value_' not in section
    assert "/readings" not in section
    assert f'href="/measurements/{gravity}?batch={batch}"' in section


def test_a_measurement_the_equipment_isnt_set_up_for_says_so(client, world):
    factory, ids = world
    _measured_batch(factory, ids)
    text = client.get(f"/batches/{ids['running_ferment']}").text
    section = text.split("<h2>Measurements</h2>")[1]
    assert "Not set up on FV01 yet." in section


def test_a_batch_type_without_measurements_shows_no_section(client, world):
    _, ids = world
    text = client.get(f"/batches/{ids['running_brew']}").text
    assert "<h2>Measurements</h2>" not in text


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


def test_viewing_pages_hold_no_forms_that_write(client, world):
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


# -- entering several measurements at once ---------------------------------


def _token(page: str) -> str:
    match = re.search(r'name="token" value="([^"]+)"', page)
    assert match, "the page has no pass"
    return match.group(1)


def _send(client, page_url, post_url, data):
    """Fill in a page's measurement boxes and press Record readings once."""
    token = _token(client.get(page_url).text)
    return client.post(post_url, data={**data, "token": token})


def _two_measurement_batch(factory, ids):
    """The measured FV09 batch, with Temperature measured as well."""
    batch, gravity = _measured_batch(factory, ids)
    with factory() as s:
        temperature = create_event_frame_attribute_template(
            s, ids["fermentation"], "Temperature"
        )
        tag = next(
            w.tag_id
            for w in s.scalars(select(EventFrameAttribute))
            if w.event_frame_attribute_template_id == temperature.id
            and s.get(Element, w.element_id).name == "FV09"
        )
        s.commit()
    return batch, gravity, tag


def _values(factory, tag_id) -> list[TagValue]:
    with factory() as s:
        return list(
            s.scalars(
                select(TagValue)
                .where(TagValue.tag_id == tag_id)
                .order_by(TagValue.observed_at, TagValue.id)
            ).all()
        )


def test_a_running_batch_takes_several_measurements_in_one_press(
    client, world
):
    factory, ids = world
    batch, gravity, temperature = _two_measurement_batch(factory, ids)
    url = f"/batches/{batch}"
    page = client.get(url).text
    assert ">Record readings</button>" in page
    assert page.count('name="observed_at"') == 1
    response = _send(
        client,
        url,
        f"{url}/readings",
        {
            f"value_{gravity}": "4.2",
            f"value_{temperature}": "18.5",
            "observed_at": "2026-01-21T07:00",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"{url}?done=readings_recorded#measurements"
    )
    for tag_id, value in ((gravity, 4.2), (temperature, 18.5)):
        made = _values(factory, tag_id)[-1]
        assert made.value == value
        assert made.observed_at == datetime(2026, 1, 21, 13, 0)
        assert made.recorded_by_id == ids["sam"]
    assert "Readings recorded." in client.get(
        response.headers["location"]
    ).text


def test_blank_boxes_are_skipped(client, world):
    factory, ids = world
    batch, gravity, temperature = _two_measurement_batch(factory, ids)
    before = len(_values(factory, gravity))
    response = _send(
        client,
        f"/batches/{batch}",
        f"/batches/{batch}/readings",
        {
            f"value_{gravity}": "4",
            f"value_{temperature}": "  ",
            "observed_at": "2026-01-21T07:00",
        },
    )
    assert response.status_code == 303
    assert len(_values(factory, gravity)) == before + 1
    assert _values(factory, temperature) == []


def test_nothing_filled_in_is_refused(client, world):
    factory, ids = world
    batch, gravity, _ = _two_measurement_batch(factory, ids)
    response = _send(
        client,
        f"/batches/{batch}",
        f"/batches/{batch}/readings",
        {"observed_at": "2026-01-21T07:00"},
    )
    assert response.status_code == 400
    assert "Enter at least one value." in response.text


def test_one_bad_value_saves_none_and_keeps_the_rest(client, world):
    factory, ids = world
    batch, gravity, temperature = _two_measurement_batch(factory, ids)
    before = len(_values(factory, gravity))
    response = _send(
        client,
        f"/batches/{batch}",
        f"/batches/{batch}/readings",
        {
            f"value_{gravity}": "4",
            f"value_{temperature}": "warm",
            "observed_at": "2026-01-21T07:00",
        },
    )
    assert response.status_code == 400
    assert len(_values(factory, gravity)) == before
    assert _values(factory, temperature) == []
    assert f'name="value_{gravity}" value="4"' in response.text
    assert f'name="value_{temperature}" value="warm"' in response.text
    assert 'value="2026-01-21T07:00"' in response.text
    # The message sits beside the box it is about.
    row = response.text.split('class="reading has-error"')[1]
    row = row.split('class="reading')[0]
    assert "Temperature" in row
    assert "Enter a number, such as 18.5." in row


def test_a_time_outside_the_batch_stops_once_then_saves(client, world):
    factory, ids = world
    batch, gravity, _ = _two_measurement_batch(factory, ids)
    url = f"/batches/{batch}/readings"
    data = {f"value_{gravity}": "4", "observed_at": "2026-01-01T07:00"}
    before = len(_values(factory, gravity))
    shown = _send(client, f"/batches/{batch}", url, data)
    assert shown.status_code == 200
    assert "That time is before this batch started." in shown.text
    assert ">Record readings anyway</button>" in shown.text
    assert len(_values(factory, gravity)) == before
    accepted = re.search(r'name="accepted" value="([^"]*)"', shown.text)
    saved = client.post(
        url,
        data={
            **data,
            "token": _token(shown.text),
            "accepted": html.unescape(accepted.group(1)),
        },
    )
    assert saved.status_code == 303
    assert len(_values(factory, gravity)) == before + 1


def test_several_without_the_pass_saves_nothing_and_keeps_the_form(
    client, world
):
    factory, ids = world
    batch, gravity, _ = _two_measurement_batch(factory, ids)
    before = len(_values(factory, gravity))
    response = client.post(
        f"/batches/{batch}/readings",
        data={f"value_{gravity}": "4", "observed_at": "2026-01-21T07:00"},
    )
    assert response.status_code == 409
    assert "Nothing was saved" in response.text
    assert f'name="value_{gravity}" value="4"' in response.text
    assert len(_values(factory, gravity)) == before


def test_a_finished_batch_takes_no_readings_this_way(client, world):
    factory, ids = world
    batch, gravity = _measured_batch(factory, ids, close=True)
    before = len(_values(factory, gravity))
    token = _token(client.get(f"/batches/{batch}").text)  # Reopen's pass
    response = client.post(
        f"/batches/{batch}/readings",
        data={
            f"value_{gravity}": "4",
            "observed_at": "2026-01-21T07:00",
            "token": token,
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/batches/{batch}"
    assert len(_values(factory, gravity)) == before


def test_boxes_for_other_tags_are_ignored(client, world):
    factory, ids = world
    batch, gravity, _ = _two_measurement_batch(factory, ids)
    with factory() as s:
        stray = s.scalars(
            select(TagValue.tag_id).where(TagValue.tag_id != gravity)
        ).first()
    response = _send(
        client,
        f"/batches/{batch}",
        f"/batches/{batch}/readings",
        {
            f"value_{gravity}": "4",
            "value_99999": "7",
            "observed_at": "2026-01-21T07:00",
        },
    )
    assert response.status_code == 303
    assert stray is None or all(v.value != 7 for v in _values(factory, stray))


def test_another_companys_batch_takes_no_readings(client, world):
    _, ids = world
    response = client.post(
        f"/batches/{ids['other_batch']}/readings",
        data={"observed_at": "2026-01-21T07:00", "token": "x"},
    )
    assert response.status_code == 404
    assert "Secret" not in response.text


def _measured_equipment(factory, ids):
    """FV10 with its own Pressure number and Status list measurements."""
    with factory() as s:
        site = s.get(Site, ids["spr"])
        area = create_area(s, site.id, "UTL", "Utilities")
        psi = create_measurement_unit(s, site.enterprise_id, "psi", "PSI")
        status = create_lookup(s, site.enterprise_id, "FV Status")
        create_lookup_value(s, status.id, "Fermenting")
        create_lookup_value(s, status.id, "Retired", is_selectable=False)
        fermenter_t = s.get(Element, ids["fv01"]).element_template_id
        create_element_attribute_template(
            s, fermenter_t, "Pressure", measurement_unit_id=psi.id
        )
        create_element_attribute_template(
            s, fermenter_t, "Status", lookup_id=status.id
        )
        fv10 = create_element(s, fermenter_t, "FV10", tag_area_id=area.id)
        s.flush()
        tags = {
            s.get(Tag, a.tag_id).name.split(".")[-1]: a.tag_id
            for a in s.scalars(select(ElementAttribute))
            if a.element_id == fv10.id
        }
        s.commit()
        return fv10.id, tags["Pressure"], tags["Status"]


def test_equipment_takes_several_measurements_in_one_press(client, world):
    factory, ids = world
    fv10, pressure, status = _measured_equipment(factory, ids)
    url = f"/equipment/{fv10}"
    page = client.get(url).text
    section = page.split("<h2>Measurements</h2>")[1].split("</section>")[0]
    assert f'name="value_{pressure}"' in section
    assert '<option value="Fermenting">' in section
    assert "Retired" not in section
    assert f'href="/measurements/{pressure}"' in section
    response = _send(
        client,
        url,
        f"{url}/readings",
        {
            f"value_{pressure}": "14.5",
            f"value_{status}": "Fermenting",
            "observed_at": "2026-01-21T07:00",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"{url}?done=readings_recorded#measurements"
    )
    assert _values(factory, pressure)[-1].value == 14.5
    with factory() as s:
        (chosen,) = _values(factory, status)
        assert s.get(LookupValue, chosen.lookup_value_id).name == "Fermenting"


def test_an_option_not_on_the_list_is_refused_beside_its_box(client, world):
    factory, ids = world
    fv10, pressure, status = _measured_equipment(factory, ids)
    url = f"/equipment/{fv10}"
    response = _send(
        client,
        url,
        f"{url}/readings",
        {
            f"value_{pressure}": "14.5",
            f"value_{status}": "Retired",
            "observed_at": "2026-01-21T07:00",
        },
    )
    assert response.status_code == 400
    assert "Choose one of the options on the list." in response.text
    assert _values(factory, pressure) == []


def test_another_companys_equipment_takes_no_readings(client, world):
    _, ids = world
    response = client.post(
        f"/equipment/{ids['other_tank']}/readings",
        data={"observed_at": "2026-01-21T07:00", "token": "x"},
    )
    assert response.status_code == 404
