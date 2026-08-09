"""Build the site-configuration workbook from the Markdown brief.

``docs/site-configuration-brief.md`` is the single source of truth for
what a configuration answer looks like. This reads the fill-in form below
its paste boundary and emits one spreadsheet tab per section, so the
workbook cannot drift from the doc: edit the brief and the next workbook
follows.

Deliberately carries no guidance. Every instruction, caption and worked
example stays in the Markdown, leaving exactly one place to read and one
place to type. The one exception is a short "Read me" tab naming the
brief, so a workbook that arrives on its own says where its manual is.

The .xlsx is written directly as zipped XML rather than through a
spreadsheet library, so the package gains no dependency for it.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

#: Where the fill-in form starts, and everything above it stops.
BOUNDARY = "Everything below goes in the chat"

#: Blank rows offered under each table when nothing prefills it.
BLANK_ROWS = 15

XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

#: Tab name and scope for each table, keyed by the start of its header
#: row. Scope drives the ``scope`` argument: lookups belong to the
#: enterprise and are answered once, everything else is per site.
SHEETS: list[tuple[str, str, str]] = [
    ("List | Options", "Lists", "enterprise"),
    ("Kind of equipment | What they are called", "Equipment", "site"),
    ("Kind of equipment | What a batch is called", "Batches", "site"),
    ("Equipment | This batch", "Nesting", "site"),
    ("Equipment | Batch | What you record", "Recorded", "site"),
    ("Kind of equipment | What you read", "Rounds", "site"),
]

#: Read-only tab naming the lookups a company already has, so a
#: further site references them instead of creating near-duplicates.
REFERENCE_TAB = "Lists you already have"

SCOPES = ("all", "enterprise", "site")

_BLURBS = {
    "all": "Covers the company and its first site.",
    "enterprise": "Company-wide answers only. Shared by every site.",
    "site": "One site. The company-wide answers are already set up.",
}

_EXAMPLE_BLURB = (
    "FILLED-IN EXAMPLE, for a brewery that does not exist. This is a "
    "reference showing what a completed workbook looks like. Do NOT "
    "upload it as your own configuration."
)

# Style indexes into the cellXfs table in _STYLES.
NORMAL, HEADER, NOTE, BOLD = 0, 1, 2, 3


def default_brief_path() -> Path:
    """Return the repo's copy of the brief.

    The deploy is a ``git clone`` plus an editable install, so the package
    sits inside the repo and ``docs/`` is two levels up. A wheel installed
    without the repo has no brief; callers get a clear error rather than a
    mangled workbook.
    """
    return (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "site-configuration-brief.md"
    )


def _demark(text: str) -> str:
    """Strip Markdown emphasis so cells read as plain text."""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text).strip()


def _match_sheet(header: list[str]) -> tuple[str, str, str] | None:
    joined = " | ".join(header)
    return next((s for s in SHEETS if joined.startswith(s[0])), None)


def _prompts(section: str) -> list[str]:
    """Return the ``- Question:`` labels in one section of the form."""
    return [
        line[2:].rstrip(":").strip()
        for line in section.split("\n")
        if re.match(r"^- \S", line) and line.rstrip().endswith(":")
    ]


def parse_form(markdown: str) -> tuple[list[str], list[str], list[list[str]]]:
    """Return (company prompts, site prompts, table header rows).

    Only the form below the paste boundary is read. The brief's guidance
    is skipped on purpose: the workbook is the data-entry surface.
    """
    after = markdown.split(BOUNDARY, 1)[1]
    form = after[after.index("## Part 1"):]
    part1, part2 = form.split("## Part 2")

    headers, buf = [], []
    for line in form.split("\n") + [""]:
        if line.startswith("|"):
            buf.append([c.strip() for c in line.split("|")[1:-1]])
            continue
        if buf:
            headers.append(buf[0])
            buf = []
    return _prompts(part1), _prompts(part2), headers


def parse_example(
    markdown: str,
) -> tuple[dict[str, str], dict[str, str], dict[str, list[list[str]]]]:
    """Return the worked example's answers and table rows.

    Answers are keyed by the same prompt text the blank form uses, so the
    filled and blank workbooks stay aligned with no second mapping.
    """
    example = markdown.split("## A worked example")[1].split(BOUNDARY)[0]
    company_part, site_part = example.split("### The site, answered")

    def answers(section: str) -> dict[str, str]:
        found = {}
        for line in section.split("\n"):
            match = re.match(r"^- (.+?): (.+)$", line.strip())
            if match:
                found[match.group(1).strip()] = match.group(2).strip()
        return found

    rows: dict[str, list[list[str]]] = {}
    buf: list[list[str]] = []
    for line in example.split("\n") + [""]:
        if line.startswith("|"):
            if not line.startswith("| ---"):
                buf.append([c.strip() for c in line.split("|")[1:-1]])
            continue
        if buf:
            sheet = _match_sheet(buf[0])
            if sheet:
                rows[sheet[1]] = buf[1:]
            buf = []
    return answers(company_part), answers(site_part), rows


def _column(index: int) -> str:
    return chr(ord("A") + index)


def _sheet_xml(
    rows: list[tuple[list[str], int]],
    widths: list[int],
    freeze: bool = False,
) -> str:
    """Render one worksheet. sheetViews, then cols, then sheetData."""
    views = (
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" '
        'topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        if freeze
        else ""
    )
    cols = "".join(
        '<col min="%d" max="%d" width="%d" customWidth="1"/>'
        % (i + 1, i + 1, w)
        for i, w in enumerate(widths)
    )
    body = []
    for number, (cells, style) in enumerate(rows, 1):
        rendered = "".join(
            '<c r="%s%d" t="inlineStr" s="%d"><is>'
            '<t xml:space="preserve">%s</t></is></c>'
            % (_column(i), number, style, escape(str(value)))
            for i, value in enumerate(cells)
            if value != ""
        )
        body.append('<row r="%d">%s</row>' % (number, rendered))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main">%s<cols>%s</cols><sheetData>%s'
        "</sheetData></worksheet>" % (views, cols, "".join(body))
    )


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/'
    'spreadsheetml/2006/main">'
    '<fonts count="3">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    '<font><i/><sz val="10"/><color rgb="FF666666"/>'
    '<name val="Calibri"/></font>'
    "</fonts>"
    '<fills count="3"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid">'
    '<fgColor rgb="FFEFEFEF"/><bgColor indexed="64"/>'
    "</patternFill></fill></fills>"
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
    '<cellXfs count="4">'
    "<xf/>"
    '<xf fontId="1" fillId="2" applyFont="1" applyFill="1"/>'
    '<xf fontId="2" applyFont="1" applyAlignment="1">'
    '<alignment wrapText="1" vertical="top"/></xf>'
    '<xf fontId="1" applyFont="1"/>'
    "</cellXfs></styleSheet>"
)

_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_NS = "http://schemas.openxmlformats.org/officeDocument/2006"


def _package(sheets: list[tuple[str, list, list[int], str]]) -> bytes:
    """Zip the rendered sheets into a minimal .xlsx package."""
    count = len(sheets)
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types"><Default Extension="rels" ContentType='
        '"application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + "".join(
            '<Override PartName="/xl/worksheets/sheet%d.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.worksheet+xml"/>' % (i + 1)
            for i in range(count)
        )
        + "</Types>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main" xmlns:r="%s/relationships"><sheets>'
        % _DOC_NS
        + "".join(
            '<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
            % (escape(sheet[0]), i + 1, i + 1)
            for i, sheet in enumerate(sheets)
        )
        + "</sheets></workbook>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="%s">' % _RELS_NS
        + "".join(
            '<Relationship Id="rId%d" Type="%s/relationships/worksheet" '
            'Target="worksheets/sheet%d.xml"/>' % (i + 1, _DOC_NS, i + 1)
            for i in range(count)
        )
        + '<Relationship Id="rId%d" Type="%s/relationships/styles" '
        'Target="styles.xml"/>' % (count + 1, _DOC_NS)
        + "</Relationships>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="%s"><Relationship Id="rId1" '
        'Type="%s/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>" % (_RELS_NS, _DOC_NS)
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/styles.xml", _STYLES)
        for i, (name, rows, widths, _scope) in enumerate(sheets):
            archive.writestr(
                "xl/worksheets/sheet%d.xml" % (i + 1),
                _sheet_xml(rows, widths, freeze=name != "Read me"),
            )
    return buffer.getvalue()


def build_workbook(
    markdown: str,
    scope: str = "all",
    example: bool = False,
    data: dict | None = None,
    existing_lists: list[tuple[str, str]] | None = None,
) -> bytes:
    """Return an .xlsx as bytes.

    Structure always comes from ``markdown``. Content comes from ``data``
    if given (keyed by tab name: ``Company`` and ``Site`` map prompt to
    answer, every other key is a list of rows), else from the brief's
    worked example if ``example``, else the tabs are left blank.

    ``existing_lists`` is (name, options) for the lookups the company
    already has. A site-scope workbook has no editable Lists tab on
    purpose, so this arrives as a read-only reference instead: without it
    someone filling in a second site cannot see that "FV Status" exists
    and invents "Fermenter Status" beside it. Lookups are
    enterprise-scoped, so that duplicate would follow every later site.
    """
    if scope not in SCOPES:
        raise ValueError("scope must be one of %s" % (SCOPES,))

    company, site, headers = parse_form(markdown)
    answers_company, answers_site, rows_by_sheet = (
        parse_example(markdown) if example else ({}, {}, {})
    )
    blurb = _EXAMPLE_BLURB if example else _BLURBS[scope]
    if data:
        answers_company = data.get("Company", {})
        answers_site = data.get("Site", {})
        rows_by_sheet = {
            k: v
            for k, v in data.items()
            if k not in ("Company", "Site", "_readme")
        }
        example = True
        blurb = data.get("_readme") or blurb

    sheets: list[tuple[str, list, list[int], str]] = [(
        "Read me",
        [(["Brewery Pi: site configuration brief"], BOLD),
         ([""], NORMAL),
         (["How to fill this in: see docs/site-configuration-brief.md. "
           "The tabs here match its sections and carry no instructions "
           "of their own."], NOTE),
         ([blurb], NOTE)],
        [104],
        "all",
    )]

    for name, prompts, sheet_scope, source in (
        ("Company", company, "enterprise", answers_company),
        ("Site", site, "site", answers_site),
    ):
        rows = [(["Question", "Your answer"], HEADER)]
        rows += [([p, source.get(p, "")], NORMAL) for p in prompts]
        sheets.append((name, rows, [46, 44], sheet_scope))

    for header in headers:
        sheet = _match_sheet(header)
        if not sheet:
            continue
        rows = [(header, HEADER)]
        if example:
            rows += [(r, NORMAL) for r in rows_by_sheet.get(sheet[1], [])]
        else:
            rows += [
                ([""] * len(header), NORMAL) for _ in range(BLANK_ROWS)
            ]
        sheets.append((sheet[1], rows, [22] * len(header), sheet[2]))

    if existing_lists:
        rows = [(["List", "Options", "Already set up?"], HEADER)]
        rows += [
            ([name, options, "yes, use it as it is"], NORMAL)
            for name, options in existing_lists
        ]
        sheets.append((REFERENCE_TAB, rows, [24, 46, 22], "site"))

    if scope != "all":
        sheets = [s for s in sheets if s[3] in ("all", scope)]
    return _package(sheets)


def build_from_brief(
    scope: str = "all",
    example: bool = False,
    data: dict | None = None,
    brief_path: Path | None = None,
    existing_lists: list[tuple[str, str]] | None = None,
) -> bytes:
    """Read the brief off disk and build the workbook from it."""
    path = brief_path or default_brief_path()
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(
            "the configuration brief is not readable at %s; the workbook "
            "is generated from it, so it must ship alongside the package "
            "(a git checkout with an editable install, as the deploy "
            "guide describes)" % path
        ) from exc
    return build_workbook(
        markdown, scope, example, data, existing_lists
    )
