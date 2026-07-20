"""The server ships a brewery-vocabulary instructions block per tier."""

import importlib
import os

import pytest

from brewerypi import mcp_server


def _reload(role):
    os.environ.pop("MCP_ROLE", None)
    if role:
        os.environ["MCP_ROLE"] = role
    importlib.reload(mcp_server)
    return mcp_server.mcp.instructions


@pytest.fixture(autouse=True)
def _restore():
    yield
    os.environ.pop("MCP_ROLE", None)
    importlib.reload(mcp_server)


def test_operator_instructions_cover_the_vocabulary():
    text = _reload(None)
    for phrase in [
        "brewery staff",
        "event frame = a batch",
        "lookup = a list of text options",
        "THE THREE SHAPES OF DATA",
    ]:
        assert phrase in text


def test_operator_instructions_scope_the_role():
    text = _reload(None)
    assert "cannot change how the brewery is set up" in text
    # setup guidance belongs to admin only
    assert "A SENSIBLE SETUP ORDER" not in text


def test_admin_instructions_add_setup_guidance():
    text = _reload("admin")
    assert "A SENSIBLE SETUP ORDER" in text
    assert "set a brewery up from nothing" in text
    # the shared vocabulary is still present
    assert "event frame = a batch" in text


def test_instructions_teach_reading_lists_not_assuming():
    for role in (None, "admin"):
        text = _reload(role)
        assert "Read the\n  list rather than assuming" in text


def test_tool_descriptions_use_brewery_language():
    from brewerypi.mcp_server import (
        create_element,
        create_element_template,
        create_event_frame,
    )

    assert "KIND of equipment" in create_element_template.__doc__
    assert "actual piece of equipment" in create_element.__doc__
    assert "Start a batch" in create_event_frame.__doc__


def test_both_tiers_are_told_to_read_house_context():
    for role in (None, "admin"):
        text = _reload(role)
        assert "call `get_house_context` once" in text


def test_admin_setup_order_ends_by_saving_house_context():
    text = _reload("admin")
    assert "offer to save their house context" in text
    assert "set_house_context" in text
    # operators are not told to write it
    assert "set_house_context" not in _reload(None)
