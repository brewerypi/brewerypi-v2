"""Tests for the site-configuration workbook generator.

The workbook is derived from ``docs/site-configuration-brief.md`` rather
than written alongside it, so these check the derivation holds: the tabs
follow the brief's tables, scope filters them, and no guidance leaks out
of the Markdown into the spreadsheet.
"""

import io
import re
import zipfile
from xml.etree import ElementTree as ET

import pytest

from brewerypi import workbook

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def tabs(content: bytes) -> dict[str, list[list[str]]]:
    """Return {tab name: rows of cell text} for a workbook's bytes."""
    archive = zipfile.ZipFile(io.BytesIO(content))
    assert archive.testzip() is None
    for name in archive.namelist():
        ET.fromstring(archive.read(name))  # every part is well-formed XML
    names = [
        s.get("name")
        for s in ET.fromstring(
            archive.read("xl/workbook.xml")
        ).iter(NS + "sheet")
    ]
    out = {}
    for index, name in enumerate(names, 1):
        sheet = ET.fromstring(
            archive.read("xl/worksheets/sheet%d.xml" % index)
        )
        rows = []
        for row in sheet.iter(NS + "row"):
            cells = [t.text or "" for t in row.iter(NS + "t")]
            if cells:
                rows.append(cells)
        out[name] = rows
    return out


@pytest.fixture
def brief() -> str:
    return workbook.default_brief_path().read_text(encoding="utf-8")


def test_brief_ships_with_the_package():
    assert workbook.default_brief_path().is_file()


def test_scope_all_covers_company_and_site(brief):
    assert set(tabs(workbook.build_workbook(brief))) == {
        "Read me", "Company", "Site", "Lists", "Equipment", "Batches",
        "Nesting", "Recorded", "Rounds",
    }


def test_enterprise_scope_drops_the_site_tabs(brief):
    names = set(tabs(workbook.build_workbook(brief, scope="enterprise")))
    assert names == {"Read me", "Company", "Lists"}


def test_site_scope_drops_the_company_wide_tabs(brief):
    """A further site must not be able to rewrite shared config."""
    names = set(tabs(workbook.build_workbook(brief, scope="site")))
    assert "Company" not in names and "Lists" not in names
    assert "Equipment" in names


def test_blank_workbook_has_headers_but_no_answers(brief):
    equipment = tabs(workbook.build_workbook(brief))["Equipment"]
    assert equipment[0][0] == "Kind of equipment"
    assert len(equipment) == 1  # blank rows carry no cell text


def test_example_fills_the_tabs_from_the_brief(brief):
    filled = tabs(workbook.build_workbook(brief, example=True))
    kinds = [row[0] for row in filled["Equipment"][1:]]
    assert "Boiler" in kinds
    batches = {row[0]: row[1:] for row in filled["Batches"][1:]}
    assert batches["Brewhouse"][1] == "Yes"  # the one non-exclusive kind


def test_example_warns_it_is_not_real_configuration(brief):
    readme = " ".join(
        c for row in tabs(
            workbook.build_workbook(brief, example=True)
        )["Read me"] for c in row
    )
    assert "does not exist" in readme and "Do NOT upload" in readme


def test_data_overrides_the_example(brief):
    content = workbook.build_workbook(
        brief,
        scope="site",
        data={
            "Site": {"Site name": "Bend"},
            "Equipment": [["Kettle", "BK1", "Brewhouse", ""]],
        },
    )
    filled = tabs(content)
    assert ["Site name", "Bend"] in filled["Site"]
    assert filled["Equipment"][1][:2] == ["Kettle", "BK1"]


def test_no_guidance_leaks_out_of_the_markdown(brief):
    """Instructions live in the brief; the workbook is for typing into.

    Every cell must come from a table row or a prompt in the brief, so a
    caption or a guidance bullet cannot reach the spreadsheet. The Read
    me tab is the one exception, and only to say where the brief is.
    """
    allowed = {"Question", "Your answer", ""}
    for line in brief.split("\n"):
        if line.startswith("|") and not line.startswith("| ---"):
            allowed.update(c.strip() for c in line.split("|")[1:-1])
        match = re.match(r"^- (.+?):(.*)$", line.strip())
        if match:
            allowed.add(match.group(1).strip())
            allowed.add(match.group(2).strip())

    filled = tabs(workbook.build_workbook(brief, example=True))
    for name, rows in filled.items():
        if name == "Read me":
            continue
        for row in rows:
            for cell in row:
                assert cell in allowed, "not from the brief, %s: %r" % (
                    name, cell,
                )


def test_unknown_scope_is_rejected(brief):
    with pytest.raises(ValueError, match="scope must be one of"):
        workbook.build_workbook(brief, scope="nonsense")


def test_missing_brief_explains_the_deploy_requirement(tmp_path):
    with pytest.raises(FileNotFoundError, match="editable install"):
        workbook.build_from_brief(brief_path=tmp_path / "nope.md")


def test_site_workbook_shows_the_lists_that_already_exist(brief):
    """Site scope has no editable Lists tab, so it needs the reference."""
    filled = tabs(workbook.build_workbook(
        brief,
        scope="site",
        existing_lists=[("FV Status", "Empty, Clean, Filling")],
    ))
    assert "Lists" not in filled
    assert filled[workbook.REFERENCE_TAB][1][:2] == [
        "FV Status", "Empty, Clean, Filling",
    ]


def test_no_reference_tab_when_nothing_exists_yet(brief):
    assert workbook.REFERENCE_TAB not in tabs(
        workbook.build_workbook(brief, scope="site")
    )


def test_units_reference_shows_symbol_and_name(brief):
    """The fill-in column wants the symbol, so show which is which."""
    filled = tabs(workbook.build_workbook(
        brief, existing_units=[("\u00b0P", "Degree Plato")],
    ))
    assert filled[workbook.UNITS_TAB][1][:2] == ["\u00b0P", "Degree Plato"]


def test_units_reference_appears_at_every_scope(brief):
    units = [("\u00b0P", "Degree Plato")]
    for scope in workbook.SCOPES:
        filled = tabs(workbook.build_workbook(
            brief, scope=scope, existing_units=units,
        ))
        assert workbook.UNITS_TAB in filled, scope


def test_site_workbook_names_its_company(brief):
    """Site.enterprise_id is required, so the workbook must carry it."""
    filled = tabs(workbook.build_workbook(
        brief, scope="site", company_name="Example Brewing Co.",
    ))
    assert [workbook.COMPANY_PROMPT, "Example Brewing Co."] in filled[
        "Site"
    ]


def test_company_prompt_is_a_real_line_in_the_brief(brief):
    """Guards the prefill key against the brief being reworded."""
    assert "- %s:" % workbook.COMPANY_PROMPT in brief
