"""Tests for running a batch's steps from its own page.

Uses the batch screens' world: Brew 88 runs on BH1 (a brewhouse, which runs
several brews at once), with its Mashing running on MM1 inside it. Times
are Chicago's, six hours behind UTC.
"""

# The world and client fixtures are imported from the batch screens' tests,
# and a test naming them as parameters "redefines" them.
# ruff: noqa: F811

import html
import re
from datetime import datetime

from sqlalchemy import select

from brewerypi.models import (
    Element,
    EventFrame,
    EventFrameAttribute,
    Site,
)
from brewerypi.services import (
    close_event_frame,
    create_area,
    create_element,
    create_element_template,
    create_event_frame,
    create_event_frame_attribute_template,
    create_event_frame_template,
)
from tests.test_web_batches import (  # noqa: F401
    _at,
    _client,
    _token,
    _values,
    client,
    world,
)


def _brewhouse(factory, ids):
    """Give BH1 a lauter tun and two kettles, and the Brew two more steps.

    LT1 stores its tags in a Brewhouse area, so Lautering's Runoff gravity
    is set up on it.
    """
    with factory() as s:
        site = s.get(Site, ids["spr"])
        area = create_area(s, site.id, "BH", "Brewhouse")
        brewhouse_t = s.get(Element, ids["bh1"]).element_template_id
        lauter_t = create_element_template(
            s, site.id, "Lauter Tun", parent_id=brewhouse_t
        )
        kettle_t = create_element_template(
            s, site.id, "Kettle", parent_id=brewhouse_t
        )
        lautering = create_event_frame_template(
            s, lauter_t.id, "Lautering", step_order=2, parent_id=ids["brew"]
        )
        boiling = create_event_frame_template(
            s, kettle_t.id, "Boiling", step_order=3, parent_id=ids["brew"]
        )
        create_event_frame_attribute_template(
            s, lautering.id, "Runoff gravity"
        )
        lt1 = create_element(
            s, lauter_t.id, "LT1", parent_id=ids["bh1"], tag_area_id=area.id
        )
        k1 = create_element(s, kettle_t.id, "K1", parent_id=ids["bh1"])
        k2 = create_element(s, kettle_t.id, "K2", parent_id=ids["bh1"])
        runoff = next(
            w.tag_id
            for w in s.scalars(select(EventFrameAttribute))
            if w.element_id == lt1.id
        )
        mashing = s.get(
            EventFrame, ids["running_mash"]
        ).event_frame_template_id
        s.commit()
        return {
            "lautering": lautering.id, "boiling": boiling.id,
            "mashing": mashing, "lt1": lt1.id, "k1": k1.id, "k2": k2.id,
            "runoff": runoff,
        }


def _steps(text: str) -> str:
    return text.split("<h2>Steps</h2>")[1].split("</section>")[0]


def _press(client, batch_id, action, at="2026-01-20T08:00", **extra):
    """Press one step's button on a batch page, with its time."""
    url = f"/batches/{batch_id}"
    token = _token(_steps(client.get(url).text))
    return client.post(
        f"{url}/steps",
        data={"action": action, "at": at, "token": token, **extra},
    )


def _frames(factory, **where) -> list[EventFrame]:
    with factory() as s:
        stmt = select(EventFrame).order_by(EventFrame.id)
        for column, value in where.items():
            stmt = stmt.where(getattr(EventFrame, column) == value)
        return list(s.scalars(stmt).all())


def test_a_brew_lists_its_steps_in_order_with_what_to_press(client, world):
    factory, ids = world
    _brewhouse(factory, ids)
    steps = _steps(client.get(f"/batches/{ids['running_brew']}").text)
    assert steps.index("Mashing") < steps.index("Lautering") < steps.index(
        "Boiling"
    )
    assert f'action="/batches/{ids["running_brew"]}/steps"' in steps
    assert 'name="at"' in steps
    assert "MM1 · running since" in steps
    assert f'value="finish:{ids["running_mash"]}">Finish Mashing<' in steps
    # Busy with this brew's own Mashing: the Finish button says enough.
    assert "MM1 is busy" not in steps
    # The next step is the main button; one kettle or the other is asked.
    assert '<button class="btn" type="submit"' in steps.split("Lautering")[1]
    assert ">Start Lautering</button>" in steps
    assert ">Start Boiling on K1</button>" in steps
    assert ">Start Boiling on K2</button>" in steps
    assert "Not started." in steps


def test_enter_in_the_time_box_presses_nothing(client, world):
    factory, ids = world
    _brewhouse(factory, ids)
    steps = _steps(client.get(f"/batches/{ids['running_brew']}").text)
    first = re.search(r"<button[^>]*>", steps).group(0)
    assert "disabled" in first


def test_starting_a_step_is_one_press(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    response = _press(
        client,
        ids["running_brew"],
        f"start:{made['lautering']}:{made['lt1']}",
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/batches/{ids['running_brew']}?done=step_started#steps"
    )
    [step] = _frames(factory, event_frame_template_id=made["lautering"])
    assert step.name == "Brew 88"
    assert step.element_id == made["lt1"]
    assert step.parent_id == ids["running_brew"]
    assert step.started_at == datetime(2026, 1, 20, 14, 0)
    assert step.started_by_id == ids["sam"]
    # Steps overlap, so the running Mashing keeps running.
    [mash] = _frames(factory, id=ids["running_mash"])
    assert mash.ended_at is None
    assert "Step started." in client.get(response.headers["location"]).text


def test_finishing_a_step_is_one_press(client, world):
    factory, ids = world
    _brewhouse(factory, ids)
    response = _press(
        client, ids["running_brew"], f"finish:{ids['running_mash']}"
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?done=step_finished#steps")
    [mash] = _frames(factory, id=ids["running_mash"])
    assert mash.ended_at == datetime(2026, 1, 20, 14, 0)
    assert mash.ended_by_id == ids["sam"]
    [brew] = _frames(factory, id=ids["running_brew"])
    assert brew.ended_at is None


def test_a_finished_step_can_be_started_again(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    _press(client, ids["running_brew"], f"finish:{ids['running_mash']}")
    steps = _steps(client.get(f"/batches/{ids['running_brew']}").text)
    mashing = steps.split("Lautering")[0]
    assert "MM1 · " in mashing and " to " in mashing
    assert ">Start another</button>" in mashing
    assert "Finish Mashing" not in mashing
    again = _press(
        client,
        ids["running_brew"],
        f"start:{made['mashing']}:{ids['mm1']}",
        at="2026-01-20T09:00",
    )
    assert again.status_code == 303
    assert len(_frames(factory, event_frame_template_id=made["mashing"])) == 2


def test_busy_equipment_says_what_it_is_busy_with(client, world):
    factory, ids = world
    _brewhouse(factory, ids)
    with factory() as s:
        other = create_event_frame(
            s, ids["bh1"], ids["brew"], "Brew 89", started_at=_at(20, 14)
        )
        s.commit()
        other_id = other.id
    steps = _steps(client.get(f"/batches/{other_id}").text)
    mashing = steps.split("Lautering")[0]
    assert "MM1 is busy with Mash 88" in mashing
    assert "Start Mashing" not in mashing
    # The next step to press skips what can't start.
    assert '<button class="btn" type="submit"' in steps.split("Lautering")[1]


def test_a_refused_step_comes_back_on_the_page(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    response = _press(
        client,
        ids["running_brew"],
        f"start:{made['lautering']}:{made['lt1']}",
        at="2026-01-19T08:00",
    )
    assert response.status_code == 400
    steps = _steps(response.text)
    assert 'class="error"' in steps
    assert 'value="2026-01-19T08:00"' in steps
    assert _frames(factory, event_frame_template_id=made["lautering"]) == []


def test_a_future_time_stops_once_and_only_for_that_press(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    start = f"start:{made['lautering']}:{made['lt1']}"
    shown = _press(client, ids["running_brew"], start, at="2099-01-01T08:00")
    assert shown.status_code == 200
    steps = _steps(shown.text)
    assert "That start time is in the future." in steps
    assert ">Start Lautering on LT1 anyway</button>" in steps
    assert _frames(factory, event_frame_template_id=made["lautering"]) == []
    accepted = html.unescape(
        re.search(r'name="accepted" value="([^"]*)"', steps).group(1)
    )
    # Accepting it for one press doesn't accept it for another.
    other = client.post(
        f"/batches/{ids['running_brew']}/steps",
        data={
            "action": f"start:{made['boiling']}:{made['k1']}",
            "at": "2099-01-01T08:00",
            "token": _token(steps),
            "accepted": accepted,
        },
    )
    assert other.status_code == 200
    assert _frames(factory, event_frame_template_id=made["boiling"]) == []
    saved = client.post(
        f"/batches/{ids['running_brew']}/steps",
        data={
            "action": start,
            "at": "2099-01-01T08:00",
            "token": _token(_steps(other.text)),
            "accepted": accepted,
        },
    )
    assert saved.status_code == 303
    assert len(_frames(factory, event_frame_template_id=made["lautering"]))


def test_a_step_without_the_pass_saves_nothing(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    response = client.post(
        f"/batches/{ids['running_brew']}/steps",
        data={
            "action": f"start:{made['lautering']}:{made['lt1']}",
            "at": "2026-01-20T08:00",
        },
    )
    assert response.status_code == 409
    assert "Nothing was saved" in response.text
    assert _frames(factory, event_frame_template_id=made["lautering"]) == []


def test_only_this_batchs_own_steps_can_be_pressed(client, world):
    factory, ids = world
    _brewhouse(factory, ids)
    for action in (
        f"finish:{ids['running_ferment']}",
        f"start:{ids['fermentation']}:{ids['fv01']}",
        "start:1",
        "launch:1",
    ):
        response = _press(client, ids["running_brew"], action)
        assert response.status_code == 404, action
    [ferment] = _frames(factory, id=ids["running_ferment"])
    assert ferment.ended_at is None


def test_a_step_with_steps_of_its_own_finishes_on_the_close_page(
    client, world
):
    factory, ids = world
    with factory() as s:
        site = s.get(Site, ids["spr"])
        mixer_t = s.get(Element, ids["mm1"]).element_template_id
        paddle_t = create_element_template(
            s, site.id, "Paddle", parent_id=mixer_t
        )
        mashing = s.get(
            EventFrame, ids["running_mash"]
        ).event_frame_template_id
        rest_t = create_event_frame_template(
            s, paddle_t.id, "Rest", step_order=1, parent_id=mashing
        )
        paddle = create_element(s, paddle_t.id, "P1", parent_id=ids["mm1"])
        create_event_frame(
            s, paddle.id, rest_t.id, "Rest", started_at=_at(20, 14),
            parent_id=ids["running_mash"],
        )
        s.commit()
    steps = _steps(client.get(f"/batches/{ids['running_brew']}").text)
    close = f'href="/batches/{ids["running_mash"]}/close">Finish Mashing<'
    assert close in steps


def test_a_step_needing_a_choice_opens_the_start_form(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    with factory() as s:
        create_event_frame_attribute_template(
            s, made["lautering"], "Run number",
            is_property=True, is_required=True,
        )
        s.commit()
    steps = _steps(client.get(f"/batches/{ids['running_brew']}").text)
    link = (
        f'/equipment/{made["lt1"]}/start?type_id={made["lautering"]}'
        f'&amp;parent_id={ids["running_brew"]}'
    )
    assert f'href="{link}"' in steps
    form = client.get(html.unescape(link)).text
    assert f'<option value="{ids["running_brew"]}" selected>' in form


def test_a_finished_batch_lists_its_steps_with_nothing_to_press(
    client, world
):
    factory, ids = world
    made = _brewhouse(factory, ids)
    with factory() as s:
        close_event_frame(s, ids["running_brew"], _at(21))
        s.commit()
    text = client.get(f"/batches/{ids['running_brew']}").text
    steps = _steps(text)
    assert "Mashing" in steps
    assert f'href="/batches/{ids["running_mash"]}"' in steps
    # Steps that never ran aren't listed once the brew is over.
    assert "Lautering" not in steps
    assert "/steps" not in steps
    response = client.post(
        f"/batches/{ids['running_brew']}/steps",
        data={
            "action": f"start:{made['lautering']}:{made['lt1']}",
            "at": "2026-01-20T08:00",
            "token": _token(text),
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/batches/{ids['running_brew']}"
    assert _frames(factory, event_frame_template_id=made["lautering"]) == []


def test_another_companys_batch_runs_no_steps(client, world):
    _, ids = world
    response = client.post(
        f"/batches/{ids['other_batch']}/steps",
        data={"action": f"finish:{ids['other_batch']}", "at": "x"},
    )
    assert response.status_code == 404


# -- a running step's measurements on its batch's page ---------------------


def _lautering(factory, ids, made) -> int:
    with factory() as s:
        step = create_event_frame(
            s, made["lt1"], made["lautering"], "Brew 88",
            started_at=_at(20, 14), parent_id=ids["running_brew"],
        )
        s.commit()
        return step.id


def test_a_brew_takes_its_running_steps_measurements(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    step = _lautering(factory, ids, made)
    url = f"/batches/{ids['running_brew']}"
    page = client.get(url).text
    section = page.split("<h2>Measurements</h2>")[1].split("</section>")[0]
    assert "Runoff gravity" in section
    assert f'name="value_{made["runoff"]}"' in section
    response = client.post(
        f"{url}/readings",
        data={
            f"value_{made['runoff']}": "14.2",
            "observed_at": "2026-01-20T09:00",
            "token": _token(section),
        },
    )
    assert response.status_code == 303
    [reading] = _values(factory, made["runoff"])
    assert reading.value == 14.2
    assert reading.recorded_by_id == ids["sam"]
    after = client.get(url).text.split("<h2>Measurements</h2>")[1]
    assert f'href="/measurements/{made["runoff"]}?batch={step}"' in after


def test_a_step_reading_before_the_step_started_names_the_step(
    client, world
):
    factory, ids = world
    made = _brewhouse(factory, ids)
    _lautering(factory, ids, made)
    url = f"/batches/{ids['running_brew']}"
    response = client.post(
        f"{url}/readings",
        data={
            f"value_{made['runoff']}": "14.2",
            "observed_at": "2026-01-20T07:00",
            "token": _token(client.get(url).text),
        },
    )
    assert response.status_code == 200
    assert "That time is before Lautering on LT1 started." in response.text
    assert _values(factory, made["runoff"]) == []


def test_a_finished_steps_measurements_stay_on_its_own_page(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    step = _lautering(factory, ids, made)
    with factory() as s:
        close_event_frame(s, step, _at(20, 16))
        s.commit()
    page = client.get(f"/batches/{ids['running_brew']}").text
    assert "Runoff gravity" not in page
    own = client.get(f"/batches/{step}").text
    assert "Runoff gravity" in own


def test_a_page_with_more_than_its_own_measurements_heads_each_group(
    client, world
):
    factory, ids = world
    made = _brewhouse(factory, ids)
    _lautering(factory, ids, made)
    with factory() as s:
        create_event_frame_attribute_template(s, ids["brew"], "Batch size")
        s.commit()
    section = client.get(f"/batches/{ids['running_brew']}").text.split(
        "<h2>Measurements</h2>"
    )[1]
    assert "<h3>Brew</h3>" in section
    assert "<h3>Lautering on LT1</h3>" in section
    assert section.index("Batch size") < section.index("Runoff gravity")


def test_another_sites_user_cant_reach_the_steps(world):
    factory, ids = world
    _brewhouse(factory, ids)
    response = _client(factory, "atl@brewery.com").get(
        f"/batches/{ids['running_brew']}"
    )
    assert response.status_code == 404
