# Design decisions

Decisions that shape the schema and services but are not (yet) visible in the
code. Built conventions live elsewhere: naming in `CONVENTIONS.md`, the
architecture summary in `CLAUDE.md`, per-release detail in `CHANGELOG.md`.

Each entry says whether it is **built** or **decided, not built**, so that
reading the code and reading this page cannot quietly disagree.

---

## Transfer frames and batch lineage

**Status: decided, not built.**

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
lineage. Tools can still let people speak in vessels: "transfer FV01 to BBT03"
resolves each vessel to the frame currently open on it.

**Multiplicity lives in the number of transfer frames.** Two brews into one
fermentation is two knockout frames sharing a destination. One fermentation
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

**Knockout always gets an explicit transfer frame.** Every lineage link goes
through a transfer, so traversal is uniform: frame -> transfer -> frame, with
no special-cased brew-to-fermentation edge. Workflow implication: closing a
brew should offer to create the knockout frame in the same action, rather than
leaving it as separate bookkeeping.

**Timing is unconstrained.** A transfer frame's window need not sit inside its
source or destination windows; real transfers straddle boundaries. A soft
warning may be worth adding later, but no hard guard.

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
2. **The destination frame must be open to receive.** If the fermentation was
   fully transferred out and closed, the return requires reopening it first.
   `reopen_event_frame` already re-runs the overlap guard, so if another
   fermentation has since started in that vessel the reopen is correctly
   refused. Reopen stays the single audited path back to an open batch; the
   transfer tools must not quietly resurrect frames.
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
conditioning -> centrifugation -> fermentation -> knockout -> brews.

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

## House context

**Status: built.**

How a brewery talks, its house rules and its rough operating ranges cannot be
derived from the data, so they are stored as free text on `Enterprise` and
read by the agent through `get_house_context`.

Storing it in the database rather than in a Claude Project is deliberate: a
Project applies only to chats inside it, so a second operator or admin would
inherit nothing, and pasted copies drift apart. On the enterprise it is
written once, inherited by every user on any plan, versioned with the data,
and changed conversationally.

Only what cannot be looked up belongs there -- vocabulary, ambiguities to
resolve, rough ranges, house rules. Sites, equipment, measurements and units
are all queryable, and duplicating them would create two sources that can
disagree. Capped at 4,000 characters (a backstop against pasted SOPs; the
real control is the tool description asking for ~400 words), with an error
that explains the cost rather than just the limit.

Writes are admin-only, on the grounds that this is shared configuration
affecting every user. Reads are on both tiers.

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
