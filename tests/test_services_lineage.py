"""Tests for walking the batch lineage (flow) graph."""

import datetime

import pytest

from brewerypi.services import (
    ValidationError,
    close_event_frame,
    create_event_frame,
    trace_lineage,
)


def D(day, hour, minute=0):
    return datetime.datetime(2026, 7, day, hour, minute)


@pytest.fixture
def chain(flow_ctx):
    """Two brews through one fermentation to a canning run.

    Brew #1 --knockout--> \\
                           FV01 --centrifugation--> BBT01 --> canning run
    Brew #2 --knockout--> /

    The last hop has no transfer frame of its own: the canning run simply
    names the brite tank as its source, which is the direct-rack shape the
    walk has to handle alongside dedicated transfer equipment.
    """
    session, ids = flow_ctx
    brew1 = create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    brew2 = create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew #2", started_at=D(17, 7)
    )
    ferm = create_event_frame(
        session, ids["fv01"], ids["ferm_t"], "FV01 B123", started_at=D(17, 5)
    )
    ko1 = create_event_frame(
        session, ids["bh1"], ids["knockout_t"], "Knockout #1",
        started_at=D(17, 8), ended_at=D(17, 9, 30),
        source_event_frame_id=brew1.id, destination_event_frame_id=ferm.id,
    )
    ko2 = create_event_frame(
        session, ids["bh1"], ids["knockout_t"], "Knockout #2",
        started_at=D(17, 10), ended_at=D(17, 11, 30),
        source_event_frame_id=brew2.id, destination_event_frame_id=ferm.id,
    )
    close_event_frame(session, brew1.id, ended_at=D(17, 9, 30))
    close_event_frame(session, brew2.id, ended_at=D(17, 11, 30))
    cond = create_event_frame(
        session, ids["bbt01"], ids["cond_t"], "BBT01 B123",
        started_at=D(24, 7),
    )
    cent = create_event_frame(
        session, ids["cf01"], ids["cent_t"], "Centrifugation",
        started_at=D(24, 8), ended_at=D(24, 10),
        source_event_frame_id=ferm.id, destination_event_frame_id=cond.id,
    )
    close_event_frame(session, ferm.id, ended_at=D(24, 10))
    can = create_event_frame(
        session, ids["cl01"], ids["can_t"], "Canning B123",
        started_at=D(26, 8), ended_at=D(26, 12),
        source_event_frame_id=cond.id,
    )
    close_event_frame(session, cond.id, ended_at=D(26, 12))
    return session, ids, {
        "brew1": brew1.id, "brew2": brew2.id, "ferm": ferm.id,
        "ko1": ko1.id, "ko2": ko2.id, "cond": cond.id,
        "cent": cent.id, "can": can.id,
    }


def names(result):
    return {f["name"] for f in result["frames"]}


def depth_of(result, name):
    return next(f["depth"] for f in result["frames"] if f["name"] == name)


def test_which_brews_are_in_this_canning_run(chain):
    session, _, f = chain
    result = trace_lineage(session, f["can"], direction="upstream")
    assert {"Brew #1", "Brew #2"} <= names(result)
    assert result["truncated"] is False
    # one hop per transfer, brite tank nearest and the brews furthest
    assert depth_of(result, "BBT01 B123") == 1
    assert depth_of(result, "Centrifugation") == 2
    assert depth_of(result, "FV01 B123") == 3
    assert depth_of(result, "Brew #1") == 5


def test_upstream_does_not_wander_forward(chain):
    session, _, f = chain
    result = trace_lineage(session, f["ferm"], direction="upstream")
    assert names(result) == {
        "FV01 B123", "Knockout #1", "Knockout #2", "Brew #1", "Brew #2",
    }


def test_downstream_from_a_brew_reaches_the_canning_run(chain):
    session, _, f = chain
    result = trace_lineage(session, f["brew1"], direction="downstream")
    assert "Canning B123" in names(result)
    assert "Brew #2" not in names(result)


def test_both_directions_sees_the_whole_chain(chain):
    session, _, f = chain
    result = trace_lineage(session, f["ferm"], direction="both")
    assert len(result["frames"]) == 8
    # every frame reported once, however many paths reach it
    assert len({frame["id"] for frame in result["frames"]}) == 8


def test_the_direct_rack_edge_names_the_receiving_batch(chain):
    """No transfer frame in the path: the canning run carries the link."""
    session, _, f = chain
    result = trace_lineage(session, f["can"], direction="upstream")
    edge = next(
        e for e in result["edges"]
        if e["to_event_frame_id"] == f["can"]
    )
    assert edge["transfer_event_frame_id"] == f["can"]
    assert edge["from_event_frame_id"] == f["cond"]


def test_a_return_transfer_does_not_loop_forever(flow_ctx):
    """Beer sent back to the fermentation it came from is the same batch."""
    session, ids = flow_ctx
    ferm = create_event_frame(
        session, ids["fv01"], ids["ferm_t"], "FV01 B123", started_at=D(24, 5)
    )
    cond = create_event_frame(
        session, ids["bbt01"], ids["cond_t"], "BBT01 B123",
        started_at=D(24, 6),
    )
    create_event_frame(
        session, ids["cf01"], ids["cent_t"], "Centrifugation",
        started_at=D(24, 8), ended_at=D(24, 10),
        source_event_frame_id=ferm.id, destination_event_frame_id=cond.id,
    )
    # ... and back again, to the ORIGINAL fermentation, which is still open
    create_event_frame(
        session, ids["cf01"], ids["cent_t"], "Return to FV01",
        started_at=D(25, 8), ended_at=D(25, 9),
        source_event_frame_id=cond.id, destination_event_frame_id=ferm.id,
    )
    result = trace_lineage(session, ferm.id, direction="both")
    assert len(result["frames"]) == 4
    assert depth_of(result, "FV01 B123") == 0
    assert result["truncated"] is False


def test_max_depth_reports_that_it_cut_the_trail(chain):
    session, _, f = chain
    result = trace_lineage(
        session, f["can"], direction="upstream", max_depth=2
    )
    assert names(result) == {
        "Canning B123", "BBT01 B123", "Centrifugation",
    }
    assert result["truncated"] is True


def test_a_batch_with_no_transfers_returns_only_itself(flow_ctx):
    session, ids = flow_ctx
    brew = create_event_frame(
        session, ids["bh1"], ids["brew_t"], "Brew #1", started_at=D(17, 6)
    )
    result = trace_lineage(session, brew.id)
    assert names(result) == {"Brew #1"}
    assert result["edges"] == []
    assert result["truncated"] is False


def test_bad_direction_and_depth_are_refused(chain):
    session, _, f = chain
    with pytest.raises(ValidationError, match="direction must be"):
        trace_lineage(session, f["can"], direction="sideways")
    with pytest.raises(ValidationError, match="max_depth"):
        trace_lineage(session, f["can"], max_depth=0)
    with pytest.raises(ValidationError, match="max_frames"):
        trace_lineage(session, f["can"], max_frames=0)


GENERATIONS = 7


@pytest.fixture
def repitched(flow_ctx):
    """Seven brews, each fermented with yeast from the one before.

    Every day a brew cools into a fresh fermentation, alternating FV01 and
    FV02, and its yeast pitch draws from the previous day's fermentation --
    which is then closed, freeing that vessel for the day after. The wort
    trail from any fermentation is three frames long; the yeast trail runs
    back to day 1.
    """
    session, ids = flow_ctx
    previous = None
    for day in range(1, GENERATIONS + 1):
        vessel = ids["fv01"] if day % 2 else ids["fv02"]
        ferm = create_event_frame(
            session, vessel, ids["ferm_t"], f"Ferm {day}",
            started_at=D(day, 5),
        )
        brew = create_event_frame(
            session, ids["bh1"], ids["brew_t"], f"Brew {day}",
            started_at=D(day, 6),
        )
        cooling = create_event_frame(
            session, ids["wc1"], ids["cool_t"], f"Cooling {day}",
            started_at=D(day, 8), parent_id=brew.id,
            destination_event_frame_id=ferm.id,
        )
        if previous is not None:
            create_event_frame(
                session, ids["yd1"], ids["pitch_t"], f"Pitch {day}",
                started_at=D(day, 8, 10), parent_id=cooling.id,
                source_event_frame_id=previous.id,
            )
        close_event_frame(session, brew.id, ended_at=D(day, 10))
        if previous is not None:
            close_event_frame(session, previous.id, ended_at=D(day, 12))
        previous = ferm
    return session, ids, previous.id


def test_the_whole_trail_is_walked_by_default(repitched):
    """Well past the old default depth of 10, and nothing is cut."""
    session, _, last = repitched
    result = trace_lineage(session, last, direction="upstream")
    assert {f"Brew {day}" for day in range(1, GENERATIONS + 1)} <= names(
        result
    )
    assert depth_of(result, "Brew 1") == 20
    assert result["max_depth"] is None
    assert result["truncated"] is False


def test_downstream_from_an_old_brew_follows_its_yeast(repitched):
    session, _, last = repitched
    first_brew = next(
        f["id"]
        for f in trace_lineage(session, last, direction="upstream")["frames"]
        if f["name"] == "Brew 1"
    )
    result = trace_lineage(session, first_brew, direction="downstream")
    assert f"Ferm {GENERATIONS}" in names(result)


def test_excluding_yeast_pitches_leaves_the_wort_trail(repitched):
    """"Whose wort is in this fermentation?" -- one brew, not seven."""
    session, ids, last = repitched
    result = trace_lineage(
        session, last, direction="upstream",
        exclude_event_frame_template_ids=[ids["pitch_t"]],
    )
    assert names(result) == {
        f"Ferm {GENERATIONS}",
        f"Cooling {GENERATIONS}",
        f"Brew {GENERATIONS}",
    }
    # left out on purpose is not the same as cut short
    assert result["truncated"] is False
    assert all(
        e["transfer_name"] != f"Pitch {GENERATIONS}" for e in result["edges"]
    )


def test_max_frames_stops_a_large_trail_and_says_so(repitched):
    session, _, last = repitched
    result = trace_lineage(
        session, last, direction="upstream", max_frames=5
    )
    assert len(result["frames"]) == 5
    assert result["truncated"] is True
    # no edge points at a frame that was left out
    kept = {f["id"] for f in result["frames"]}
    assert all(
        {e["from_event_frame_id"], e["to_event_frame_id"]} <= kept
        for e in result["edges"]
    )
