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
    TagValue,
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
    """Press one step's button on a batch page, with the page's time."""
    url = f"/batches/{batch_id}"
    token = _token(client.get(url).text)
    return client.post(
        f"{url}/steps",
        data={"action": action, "observed_at": at, "token": token, **extra},
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
    page = client.get(f"/batches/{ids['running_brew']}").text
    steps = _steps(page)
    assert steps.index("Mashing") < steps.index("Lautering") < steps.index(
        "Boiling"
    )
    assert f'formaction="/batches/{ids["running_brew"]}/steps"' in steps
    # One time for the whole page, its steps and its readings alike.
    assert page.count('name="observed_at"') == 1
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


def _first_button(page: str) -> str:
    form = page.split('id="batch-form"')[1]
    return re.search(r"<button[^>]*>", form).group(0)


def test_enter_presses_no_step_button(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    # Nothing to record: Enter does nothing at all.
    page = client.get(f"/batches/{ids['running_brew']}").text
    assert "disabled" in _first_button(page)
    # With boxes on the page, Enter records them and starts nothing.
    _lautering(factory, ids, made)
    first = _first_button(client.get(f"/batches/{ids['running_brew']}").text)
    assert "disabled" not in first
    assert "action" not in first


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
    assert 'class="error"' in response.text
    assert 'value="2026-01-19T08:00"' in response.text
    assert _frames(factory, event_frame_template_id=made["lautering"]) == []


def test_a_future_time_stops_once_and_only_for_that_press(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    start = f"start:{made['lautering']}:{made['lt1']}"
    shown = _press(client, ids["running_brew"], start, at="2099-01-01T08:00")
    assert shown.status_code == 200
    steps = shown.text
    assert "That time is in the future." in steps
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
            "observed_at": "2099-01-01T08:00",
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
            "observed_at": "2099-01-01T08:00",
            "token": _token(other.text),
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
            "observed_at": "2026-01-20T08:00",
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
            "observed_at": "2026-01-20T08:00",
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
        data={"action": f"finish:{ids['other_batch']}"},
    )
    assert response.status_code == 404


# -- each step's measurements, under it ----------------------------------


def _lautering(factory, ids, made) -> int:
    with factory() as s:
        step = create_event_frame(
            s, made["lt1"], made["lautering"], "Brew 88",
            started_at=_at(20, 14), parent_id=ids["running_brew"],
        )
        s.commit()
        return step.id


def _row(steps: str, name: str) -> str:
    """One step's part of the steps list, up to the next step."""
    part = steps.split(f'<span class="batch-name">{name}</span>')[1]
    return part.split('<span class="batch-head"><span class="vessel">')[0]


def test_a_running_steps_measurements_sit_under_it(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    step = _lautering(factory, ids, made)
    url = f"/batches/{ids['running_brew']}"
    page = client.get(url).text
    lautering = _row(_steps(page), "Lautering")
    assert "LT1 · running since" in lautering
    assert "Runoff gravity" in lautering
    assert f'name="value_{made["runoff"]}"' in lautering
    assert "Runoff gravity" not in _row(_steps(page), "Mashing")
    response = client.post(
        f"{url}/readings",
        data={
            f"value_{made['runoff']}": "14.2",
            "observed_at": "2026-01-20T09:00",
            "token": _token(page),
        },
    )
    assert response.status_code == 303
    [reading] = _values(factory, made["runoff"])
    assert reading.value == 14.2
    assert reading.recorded_by_id == ids["sam"]
    after = _row(_steps(client.get(url).text), "Lautering")
    assert f'href="/measurements/{made["runoff"]}?batch={step}"' in after


def test_the_batchs_own_measurements_stay_above_its_steps(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    _lautering(factory, ids, made)
    with factory() as s:
        create_event_frame_attribute_template(s, ids["brew"], "Batch size")
        s.commit()
    page = client.get(f"/batches/{ids['running_brew']}").text
    own = page.split("<h2>Measurements</h2>")[1].split("</section>")[0]
    assert "Batch size" in own
    assert "Runoff gravity" not in own
    assert page.index("<h2>Measurements</h2>") < page.index("<h2>Steps</h2>")


def test_finishing_a_step_saves_what_was_typed_with_it(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    step = _lautering(factory, ids, made)
    response = _press(
        client,
        ids["running_brew"],
        f"finish:{step}",
        at="2026-01-20T09:30",
        **{f"value_{made['runoff']}": "13.8"},
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith(
        "?done=step_finished_recorded#steps"
    )
    [reading] = _values(factory, made["runoff"])
    assert reading.value == 13.8
    assert reading.observed_at == datetime(2026, 1, 20, 15, 30)
    [done] = _frames(factory, id=step)
    assert done.ended_at == datetime(2026, 1, 20, 15, 30)
    text = client.get(response.headers["location"]).text
    assert "Step finished and readings recorded." in text


def test_a_bad_value_stops_the_step_too(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    step = _lautering(factory, ids, made)
    response = _press(
        client,
        ids["running_brew"],
        f"finish:{step}",
        **{f"value_{made['runoff']}": "lots"},
    )
    assert response.status_code == 400
    assert "Enter a number, such as 18.5." in response.text
    assert f'name="value_{made["runoff"]}" value="lots"' in response.text
    [still] = _frames(factory, id=step)
    assert still.ended_at is None
    assert _values(factory, made["runoff"]) == []


def test_a_refused_step_saves_none_of_the_readings(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    _lautering(factory, ids, made)
    response = _press(
        client,
        ids["running_brew"],
        f"start:{made['boiling']}:{made['k1']}",
        at="2026-01-19T08:00",
        **{f"value_{made['runoff']}": "14"},
    )
    assert response.status_code == 400
    assert _values(factory, made["runoff"]) == []
    assert _frames(factory, event_frame_template_id=made["boiling"]) == []


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


def test_a_finished_step_sums_up_its_measurements_under_it(client, world):
    factory, ids = world
    made = _brewhouse(factory, ids)
    step = _lautering(factory, ids, made)
    with factory() as s:
        s.add(
            TagValue(
                tag_id=made["runoff"], observed_at=_at(20, 15), value=14.0
            )
        )
        close_event_frame(s, step, _at(20, 16))
        s.commit()
    lautering = _row(
        _steps(client.get(f"/batches/{ids['running_brew']}").text),
        "Lautering",
    )
    assert "Runoff gravity" in lautering
    assert "1 reading" in lautering
    assert f'href="/measurements/{made["runoff"]}?batch={step}"' in lautering
    assert 'name="value_' not in lautering


def test_another_sites_user_cant_reach_the_steps(world):
    factory, ids = world
    _brewhouse(factory, ids)
    response = _client(factory, "atl@brewery.com").get(
        f"/batches/{ids['running_brew']}"
    )
    assert response.status_code == 404
