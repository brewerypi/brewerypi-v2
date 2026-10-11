# Design decisions

Decisions that shape the schema and services but are not (yet) visible in the
code. Built conventions live elsewhere: naming in `CONVENTIONS.md`, the
architecture summary in `CLAUDE.md`, per-release detail in `CHANGELOG.md`.

Each entry says whether it is **built** or **decided, not built**, so that
reading the code and reading this page cannot quietly disagree.

---

## Transfer frames and batch lineage

**Status: built.** Columns on `event_frames`, `parent_flow` on
`event_frame_templates`, flow rules in `services/event_frames.py`, the graph
walk in `services/lineage.py`, and the `link_transfer` /
`trace_batch_lineage` tools on the operator tier.

Beer moves between vessels, and those movements are what tie one batch to the
next: several brews knock out into one fermentation, a fermentation is
centrifuged into a brite tank, part of a fermentation goes to one brite and
part to another. Today the model has no way to record any of that -- event
frames nest (a Mashing inside a Brew) but nothing connects a Brew to the
Fermentation it fed.

### The model

A transfer is **itself an event frame**, on the equipment that performs it,
with two new nullable self-referencing columns:

```
source_event_frame_id       -> event_frames.id
destination_event_frame_id  -> event_frames.id
```

A centrifugation from FV01 to BBT03 is an event frame on the centrifuge whose
source is FV01's fermentation frame and whose destination is BBT03's
conditioning frame.

Modelling a transfer as a frame rather than as a pointer between frames is the
key choice. A transfer has a duration, runs on real equipment, and carries its
own measurements (flow rate, turbidity, dissolved oxygen pickup) -- all of
which the existing event frame machinery already provides.

### Why these details

**Point at event frames, not elements.** `FV01` has held hundreds of
fermentations, so "from FV01" identifies nothing. Frame-to-frame gives real
lineage. People still choose in vessel terms: `list_event_frames` filters by
kind of equipment and kind of batch (`element_template_id` +
`event_frame_template_id` + `open_only`) and each row names its vessel, so
picking a source reads as "the fermenters -> fermentations -> FV01 B123"
rather than as an id lookup.

**Multiplicity lives in the number of transfer frames.** Two brews into one
fermentation is two wort cooling frames sharing a destination. One fermentation
split across two brite tanks is two transfer frames sharing a source. A
partial transfer is a frame that simply did not move everything. No join
table is needed, because the physical multiplicity *is* the number of
physical transfers.

**Destination is set late.** When a transfer starts, the receiving frame may
not exist yet. Both columns are nullable; expect the destination to be filled
in when the transfer closes or when the receiving frame opens.

**Volume is an attribute, not a column.** How much beer moved is essential for
yield and loss, but it is a measurement -- so it is an event frame attribute
on the transfer frame (numeric, BBL or hL), and flows into the historian like
any other reading. No schema work.

**Source/destination are NOT `parent_id`.** `parent_id` is *containment* (a
Mashing inside a Brew) and is governed by the A1 mirror rule. Source and
destination are *flow* between siblings. Two different relationships with two
different rule sets; conflating them would break A1.

**Available on any frame, not a special "transfer" template.** A direct
FV -> BBT rack with no equipment in the path can just set `source` on the
conditioning frame, with no ceremonial transfer frame. A dedicated transfer
frame earns its place when real equipment is involved, or when splitting or
blending needs one row per stream.

### Rules

**A transfer is a step of a batch when it has only one possible partner at
that end; otherwise it is a batch of its own.** Wort cooling is the last step
of a brew and can only ever draw from that brew, so it is a child frame of the
Brew (on the wort cooler, inside the brewhouse) rather than a sibling
"knockout" batch. A yeast pitch is dosed into one wort stream, so it is a
child of the Wort cooling. A centrifugation runs on equipment that belongs to
neither the fermentation nor the brite tank, so it stays a top-level frame
with both ends chosen.

This reverses the earlier rule that knockout always gets its own top-level
transfer frame, created as the brew closes. That shape could not survive the
containment rule below (a transfer created at the brew's close no longer fits
inside the brew), and it asked operators to re-enter, on every brew, a source
that the equipment layout already fixes. Nesting gets both for free: a child
must sit inside its parent's window, so "the brew stays open until the wort
has left" is structural rather than a convention to remember.

**The implied end is declared once, on the template, and stored on every
frame.** `event_frame_templates.parent_flow` says which end of the transfer
the parent batch is: `source` (Wort cooling draws from its Brew) or
`destination` (Yeast pitch delivers into its Wort cooling). The service fills
that end in when the frame is created -- before the other end is known, which
matters because a wort cooling routinely starts before anyone has picked the
fermenter -- and refuses to clear it or point it elsewhere. Only the other end
is ever chosen: which fermentation the wort goes to, which fermentation the
yeast comes from. (A yeast propagation is an ordinary fermentation batch, so
"wort only, into a propagation" is just a second Wort cooling in the same brew
with a different destination, and needs no special case.)

Two alternatives were rejected. *Inferring* the parent at trace time would
keep the link out of the table, but it would make lineage walk containment as
well as flow, and those are kept apart for good reason (see below). *Asking
for it* on every frame keeps the walk pure but makes a forgotten source a
silent hole in the lineage. Declaring it on the template and writing it to the
frame keeps the lineage walk a plain walk over recorded links with nothing to
forget. The cost is that a frame can now be both nested in and linked to the
same parent, which the close path has to know about:

**Closing a batch closes the transfers inside it.** A separate transfer still
running out of a brew blocks the brew's close. A step of the brew cannot, or
no brew with a wort cooling could be closed in one action -- so a transfer
that is among the descendants being closed in the same call is judged at the
instant it will end, where it still fits. Nesting wins over flow in exactly
this one place.

Changing a template's `parent_flow` never rewrites frames that already exist.
They keep the links they were given and pick the rule up the next time
`link_transfer` touches them.

**Timing is contained at both ends.** A transfer's window must sit *within*
each endpoint's window: beer cannot leave a batch that had not started or had
already finished. An open endpoint runs to +infinity, so it constrains only
the start; an open transfer runs to +infinity too, so it needs an open
endpoint at each linked end. One rule then covers what look like two: closing
a batch while a transfer is still running out of (or into) it is refused
because the open transfer no longer fits.

This makes frame boundaries a **configuration convention** rather than a free
choice, which is the real cost of the rule. A knockout only links if the brew
stays open until the knockout finishes and the receiving fermentation is
already open when it starts. Both match how the vessels are actually used --
the fermentation is claimed before wort arrives, and the brew day is not over
while wort is still moving -- but a brewery that closes its brew when the
kettle empties will hit a refusal and have to move that boundary.

Containment is re-checked whenever an endpoint's window moves or closes, so
the database never holds a link that the create path would have rejected.
Deleting is refused the same way: a batch cannot be deleted while a transfer
points at it, and the guard covers the whole subtree a delete would cascade
through, since deleting a Brew takes its Mashing with it. Deleting the
*element* is refused too, for the same reason. Unlink first, deliberately --
lineage is the one thing a delete must not quietly destroy.

**The flow graph genuinely cycles, and that is legitimate.** Beer can go
FV -> BBT for conditioning and later come back BBT -> FV. The returning beer
is *the same batch*, so the return transfer points at the **original**
fermentation frame -- not a new one. Creating a second fermentation frame
would invent a batch that never existed, double-count volume, and break
traceability.

Three consequences follow:

1. **Visited-set traversal is mandatory, not defensive.** Any lineage walk
   must track visited frames or it will loop forever on a re-blend. Same
   pattern as `_subtree` and `build_tag_name` in the services, but here it is
   load-bearing rather than belt-and-braces.
2. **A batch expecting beer back stays open**, and that is what keeps the
   lineage honest: while the fermentation frame is open, the exclusive-element
   overlap guard holds FV01 against any new fermentation, so there is no way
   to accidentally invent a second batch for the returning beer. Containment
   then accepts the return with no special case -- the fermentation started
   long before, and its open end imposes no upper bound. If the frame *was*
   closed, `reopen_event_frame` is the correction path (and re-runs the
   overlap guard, so a vessel that has since been refilled correctly refuses);
   the transfer tools never quietly resurrect a frame.
3. **Volume accounting must net inbound against outbound.** Summing inbound
   transfers to derive batch size would over-count returned volume. Any yield
   or loss metric has to treat transfers as directional, or it will produce
   plausible-but-wrong numbers.

**Containment stays strictly acyclic** (A1-enforced), while **flow is
permissive and cycle-tolerant**. Keeping the two rule sets apart is what makes
this safe.

### What it unlocks

The traceability query a brewery cannot get out of a spreadsheet: *"which
brews are in this canning run?"* walks the graph backward through packaging ->
conditioning -> centrifugation -> fermentation -> wort cooling -> brews.

`trace_lineage` returns that walk flat -- every frame reached with the depth
at which it was first seen, plus the edges between them -- because a graph
that re-blends and cycles has no faithful tree form. Each edge names the frame
that *carries* the link, which is the transfer itself, so a dedicated
centrifugation frame and a direct rack (the receiving frame simply naming its
source) read through the same structure.

**The walk runs to the end of the trail, and is bounded by size, not depth.**
A trace that stops partway is no use for traceability, so `max_depth` is
optional and unset by default (it was 10, capped at 50). What makes a trail
large is yeast. Every yeast pitch links a fermentation to the one its yeast
was cropped from, so a walk that follows yeast reaches back through every
generation to the last time fresh yeast came in, and forward from an old brew
to nearly everything brewed since. That is a true answer and sometimes the
wanted one, but it is not the answer to "whose wort is in this can". So:

- `exclude_event_frame_template_ids` leaves a kind of transfer out of the
  walk. Passing the Yeast pitch template asks the wort question; passing
  nothing asks for everything connected. It is a list of templates rather
  than a "yeast" flag because the schema has no notion of yeast -- that is
  the brewery's name for one of its batch types -- and the same lever serves
  any other stream a brewery wants to set aside (a blend-back, a CIP return).
- `max_frames` (default 500, ceiling 5000) stops a walk that is simply too
  big to return.
- `truncated` is true when a *limit* left part of the trail unwalked, and
  false when frames were left out by an exclusion, which is a choice, not a
  loss. A clipped graph is never mistaken for a complete one.

One thing the containment rule now asks of yeast: the fermentation a pitch
draws from must still be open when the pitch happens. A brewery that closes a
fermentation when the beer leaves and crops the cone afterwards will be
refused, and has to hold the batch open until its yeast is used. Left as is
deliberately; revisit if it proves to fight real practice.

---

## Vocabulary layer

**Status: decided, partly built.**

Users are brewery staff, not database users. The agent speaks their language;
the schema keeps its own names.

- `Enterprise` = company. `Site` = the location name ("Atlanta", "Brew 1").
  `Area` = a physical zone (brewhouse, cellar, packaging) or a virtual one
  (Utilities). `Element` = equipment carrying more than one measurement.
  `Tag` = a measurement or data point. `MeasurementUnit` = unit of measurement.
- `Lookup` is called a **"list"** in conversation ("the FV Status list"), and
  its values are "options". The table keeps the name `Lookup`; only the
  vocabulary translates. Renaming the table would be churn for no gain.
- The three data shapes, which is the distinction that actually matters:
  a **tag alone** is continuous data; an **element with element attributes**
  is grouped continuous data; an **event frame with event frame attributes**
  is batch data.
- Batch names vary by equipment: a brewhouse batch is a **brew**, an FV batch
  a **fermentation**, a centrifuge batch a **centrifugation**, a BBT batch
  **bright beer** or **conditioning**, a packaging batch a **canning run**,
  **bottling run** or **kegging run**.
- Never *lead* with "element", "event frame", "tag" or "attribute", but mirror
  those terms back when the user uses them first. Some users know PI and will
  be annoyed by translation.
- Units follow the brewery: gravity in Plato or specific gravity, temperature
  in F or C. Echo whatever the tag carries; do not normalise.

---

## Timezones and the OAuth seam

**Status: built, with one branch deliberately left empty.**

Readings are stored UTC and converted at the MCP tool boundary; the model
never does timezone arithmetic. `resolve_timezone(session, site)` in
`brewerypi/timezones.py` is the single seam: today it returns the site's
timezone, and it carries a `TODO(oauth)` branch for the authenticated user's
own zone, which will drive **both** entry and display once identity exists.

Every tool routes through that function, so the OAuth cutover is a
one-function change rather than a refactor. The interim gap is bounded and
understood: on-site users are correct today; remote users see site-local time
until identity lands.

---

## Delete philosophy

**Status: built.**

Two principles govern every delete guard, and they explain why the rules are
not uniform:

**Explicit intent versus incidental cleanup.** `delete_tag` is explicit -- the
user named that tag -- so it is allowed, cascades its readings, and protects
via an informed-consent preview (count and date range) rather than refusal.
Unwiring is incidental -- the user asked to remove a *link* -- so it never
refuses over data it was not asked to touch; it removes an owned tag only when
disposable and otherwise leaves it standing.

**Durable structure refuses; episodes cascade.** `element_templates`,
`event_frame_templates` and `elements` all refuse to delete while they have
children, because a child has independent meaning and losing a subtree is
expensive. `event_frames` cascade to their nested frames, because a Mashing
has no life apart from its Brew. Same reasoning will apply to any future
self-referential table.

**Flow links refuse, containment cascades** -- the same distinction inside one
table. A frame's `parent_id` children go with it, while a frame that a
transfer points at cannot be deleted at all, because that link records
something that physically happened between two batches and no other row
carries it. The guard tests the whole subtree the cascade would reach, so
deleting a Brew is refused when the transfer points at its Mashing.

Readings are the one thing nothing destroys by accident: `tag_values.tag_id`
is NOT NULL, so readings cannot outlive their tag, and `delete_tag` behind a
confirm preview is the only bulk path that removes them.

---

## Earmarked, not yet designed

- **OAuth / per-user identity.** The next major workstream. Scopes the
  operator write surface (event frames gave operators create/close/delete),
  fills the timezone seam, and enables attribution -- batches in particular
  want an author.
- **Computed metric tools.** Define domain metrics once as deterministic MCP
  tools (temperature deviation, batch yield, attenuation) so the agent
  orchestrates trustworthy building blocks instead of improvising arithmetic.
  This is the main lever on agent answer quality.
- **Recipe specifications.** Target ranges per brand, enabling
  in-spec/out-of-spec reasoning rather than raw numbers.
- **Postgres / TimescaleDB.** `tag_values` is the high-volume table that will
  eventually justify the move. Note that several auto-generated foreign key
  names already exceed Postgres's 63-character identifier limit; harmless on
  SQLite, but they will be truncated on migration.
- **Staff-authored forms and reports (Claude Artifacts + MCP).** A published
  Artifact can declare the `mcp` capability and call the viewer's connectors
  from the page itself, so a round-entry form or a shift report reads and
  writes Brewery Pi directly, without the data passing through the
  conversation. The plumbing already exists end to end: the deployed
  operator/admin connectors, the capability, and a tool surface covering both
  halves -- `record_tag_value` and the event frame lifecycle for writes,
  `browse_hierarchy` / `get_tag_values` / `tag_value_stats` for reads. Two
  things gate it. **Identity:** forms make writing easy and high-volume,
  which is exactly when a shared secret path with no attribution stops being
  acceptable, so this waits on OAuth above. **Distribution:** a page
  declaring `mcp` cannot be shared publicly and each viewer must add the
  connector themselves, so every brewer authors their own page rather than
  the cellar sharing one URL. Worth doing first either way: the MCP tools
  declare no `annotations`, and adding `readOnlyHint` would let read tools
  cache and coalesce while making write tools structurally un-cacheable and
  un-watchable.
