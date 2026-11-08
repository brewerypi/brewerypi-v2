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

**Status: built.**

Readings are stored UTC and converted at the MCP tool boundary; the model
never does timezone arithmetic. `resolve_timezone(session, site)` in
`brewerypi/timezones.py` is the single seam: the signed-in user's own zone
(`users.timezone`) when they have set one, else the site's, else UTC. The
user's zone drives **both** entry and display, so a remote brewmaster reads
and types times in their own clock.

Every tool routes through that function, so the identity cutover was a
one-function change rather than a refactor. A user with no zone set sees
site-local time, which is right for on-site staff -- leave it blank for them.

---

## Identity and access

**Status: built (identity, scoping and attribution).**

### Who signs in, and how

Staff sign in through **WorkOS AuthKit**, which offers Google, Microsoft and
an email magic link on one page -- breweries split between Gmail, Microsoft
365 and plain email addresses, and no single social login covers all three.
AuthKit runs the OAuth flow, including the dynamic client registration that
Claude's custom connectors use; the MCP server is only a resource server
that verifies AuthKit's JWTs (FastMCP's `AuthKitProvider`).

A person is **their verified email**. The token carries only WorkOS's user
id (`sub`), so the first time a subject is seen the server asks the WorkOS
API for that user's email, requires it to be verified, matches it to a row
in `users`, and stores the subject in `users.auth_subject`. Later calls
match on the subject alone. The column is provider-neutral on purpose:
switching identity providers means clearing the subjects, and everyone
re-links by email on their next sign-in.

### Signing in to the web app

The web app is a confidential client of the same AuthKit, so a person is the
same person on every consumer, matched the same way by verified email. The
differences follow from it being a browser application rather than a resource
server. It runs the authorization code flow itself, and WorkOS hands back the
email directly, so there's no separate lookup.

**The cookie holds who, never what.** The signed session cookie carries only
the WorkOS subject and session id. Each request resolves the subject against
`users` again, as the MCP tiers do on each tool call, so deactivating someone
or removing their access applies on their next click rather than at the next
sign-in. That is one query per request, which is a good price for access that
can't go stale.

**Signing out ends the AuthKit session too.** Clearing only our cookie would
leave AuthKit signed in, and on a tablet shared across a shift the next person
to press Sign in would become the previous person without being asked. Pages
are `no-store` and also reload themselves when the browser restores them on
Back, because a restore from memory never reaches the server.

**No way to run it with sign-in off.** `MCP_AUTH=none` exists for local work on
the tools, and it means "unrestricted". A browser app on a shared network is a
worse place for that switch, so the web app doesn't have one. Local work signs
in through the WorkOS Staging environment.

### Changing batches from the web

Every web write but a delete is **one form and one press**. Staff asked for
fewer clicks more than once, and a review page doubled every write. A delete
still goes to a confirm page, because it leaves no trace.

**Pressing runs the real write.** A refusal comes back on the same form, in
the service's own words, with what was typed kept, and the web layer holds no
copy of the batch rules to drift from them. The services leave the
transaction to their callers, so a refused or warned write rolls back whole.

**Only a warning stops a save** (a time in the future, a reading outside its
batch). The form comes back with the warning and a "... anyway" button.
Pressing it accepts those exact warnings, which the form sends back: a value
retyped into a different warning stops once more, so nothing is saved
against a warning nobody saw. Whatever a review page used to tell people is
on the form itself, such as the steps and end values a close records.

**Every form carries a pass spent on use**, kept in the session. One
mechanism covers two risks: a double tap can't start two batches, and
another site can't submit the form. A sign-in keeps its last twenty passes,
so several open tabs each work. A spent or aged-out pass brings the form
back with what was typed and "Nothing was saved", never a lost entry. A
small script also drops a second tap before it is sent, which would
otherwise replace the saved result with that message.

**Several measurements in one press.** A running batch's page, and a piece
of equipment's page, show a box per measurement under one shared time.
Blank boxes are skipped. It is all or nothing: one bad value saves none,
and the page comes back with the message beside that box and every value
kept, so nobody has to work out which half of a round went in. Boxes start
empty rather than holding the latest value, which would make recording
yesterday's gravity again one careless press away; the latest is shown
beside each box instead. A finished batch has no boxes: its readings are
corrected one at a time, from See all.

**Plain forms, no HTMX.** HTMX was the plan, but a form followed by a full page
does this job with no JavaScript dependency to keep current. It remains an easy
addition if a screen ever needs to update in place.

### Provisioning: an allowlist

AuthKit will authenticate anyone, so authentication grants nothing. A
signed-in person with no `users` row, an inactive one, or no membership is
refused on every tool call, with a message saying to ask an administrator.
The first account is created on the server with the `brewerypi-users` CLI
(the bootstrap: admin tools need an admin to exist); after that the CLI or,
from phase 2, admin tools add people.

`is_active = False` is how someone leaves. Deleting a user is refused once
any reading or batch names them -- a departed brewer's batches still need an
author. Someone who never recorded anything, such as a mistyped email, can
still be deleted.

### Roles: per enterprise, optionally narrowed to sites

```
users        1--* memberships *--1 enterprises      role: operator | admin
memberships  1--* membership_sites *--1 sites       when has_all_sites = 0
```

A membership gives one user one role in one enterprise; a user may belong
to several enterprises with a different role in each. `has_all_sites`
(default true) means every site, including ones created later -- nobody has
to be re-granted when a brewery opens a taproom. When false, the user sees
only the sites listed in `membership_sites`.

The explicit flag, rather than "no rows means all sites", is deliberate:
deleting a site cascades its grants, and under the implicit rule a user
restricted to just that site would silently be promoted to all of them.
With the flag, an emptied restriction means no sites, which fails closed.

Sites are a clean boundary because everything below one -- areas,
equipment, readings, batches -- belongs to exactly one site, and transfers
are already required to be same-site, so lineage never crosses it.
Measurement units and lists are enterprise-wide: site-restricted members can
read them, but changing them, creating sites, or changing the enterprise
needs an **unrestricted admin**.

`users.is_superuser` sits above enterprises. It exists because creating an
enterprise cannot require being an admin *of* it; superusers also manage
users server-wide.

### Tiers stay two processes

The operator and admin connectors remain separate processes. The tier
decides **which tools are offered** (operators should not carry ~80 tools
in context); the user's memberships decide **what they may do**. Everyone
signs in as themselves on either connector. The admin process refuses
anyone who is neither a superuser nor an admin somewhere.

Each tier is its own OAuth protected resource, and discovery lives under
`/.well-known/` at the host root, so in deployment each tier gets its own
hostname rather than a path prefix -- `operator.mcp` and `admin.mcp`, each
named for its tier. A connector's display name in Claude is the user's own
label and says nothing about where it points, so the URL has to: a bare
`mcp` for the operator tier invited adding the admin URL under an
"operator" name, which happened in the first test.

### Phases

1. **Identity (built).** `users` / `memberships` / `membership_sites`, the
   `brewerypi-users` CLI, AuthKit sign-in on both tiers, the allowlist and
   admin-tier gate, and the user's timezone in `resolve_timezone`.
2. **Scoping (built).** Every list/get filters to the caller's enterprises
   and sites; every write checks the caller's role where the row lives;
   user and membership admin tools. See *How scoping is enforced* below.
3. **Attribution (built).** Who recorded a reading, who started or closed a
   batch, and who last changed either. See *How attribution is recorded*
   below.

### How scoping is enforced

Every row lives at a **place** -- an enterprise, and for everything below
a site, one site. `brewerypi.access.locate` finds it by walking foreign keys
upward, and one function, `allowed`, compares the place with the caller's
grants. Keeping the decision in one module, rather than in each tool,
means one table of rules to read and one to test.

| Level | Needs | Used for |
|---|---|---|
| read | any membership; the site, if the row has one | lists, gets |
| operate | read (every role) | readings, batches, transfers |
| configure | admin at the row's site; an admin of **all** sites when the row has none | setup below the company; the company, its lists and units |
| configure enterprise | admin of all sites | creating or deleting a site, managing people |
| superuser | `is_superuser` | creating or deleting a company, server-wide user fields |

A row outside the caller's companies or sites is reported as **not found**,
in the same words as a missing id, so another company's ids reveal nothing.
A row they can see but not change is refused with a reason. Site-restricted
admins configure their own site, but deleting it is company-level: it
changes what everyone else in the company can see.

Each MCP tool declares the rows it names, with the level it needs on each
(`@_guard(tag_id=(Tag, OPERATE))`), and lists filter their results. A test
fails any tool that declares nothing, so a new tool cannot ship unscoped by
omission. Checking only the named rows is enough because the services
already require referenced rows to be in the same site or company -- a
transfer's ends, a tag's list, an element's tag area -- so a call cannot
reach across the boundary through a row it did not name.

**People.** Managing who belongs to a company takes an admin of every site:
a site-restricted admin able to grant memberships could grant themselves
the rest. Admins see only the people in companies they administer, and
add someone in two steps -- `create_user` (the sign-in list, which grants
nothing) then `create_membership` by email. Fields that apply across every
company -- deactivation, superuser, resetting a sign-in -- need a
superuser; anyone may change their own name and timezone. Memberships are
snapshotted at the start of each call, so a change applies on the person's
next request without reconnecting.

When `MCP_AUTH=none` the server runs with no sign-in at all -- every tool
open, as before identity existed. It is for local development and tests
only, and it must be set explicitly: a server with neither AuthKit
configured nor `MCP_AUTH=none` refuses to start, so a forgotten environment
variable fails closed instead of publishing an open database.

### How attribution is recorded

Authorship lives in columns on the rows themselves, not in a separate log:

| Table | Columns |
|---|---|
| `tag_values` | `recorded_by_id`, `updated_by_id` |
| `event_frames` | `started_by_id`, `ended_by_id`, `updated_by_id` |

All are nullable foreign keys to `users`. **NULL means not known**, never
"nobody": rows from before attribution stay NULL rather than being credited
to someone by guesswork, and a server run with `MCP_AUTH=none` has nobody to
name.

**A change keeps the author.** Correcting a reading or moving a batch's times
leaves `recorded_by_id` / `started_by_id` alone and sets `updated_by_id`.
Otherwise someone would be shown as having recorded a value they never
entered, or the corrector would erase who took the original reading. A call
that changes nothing records nothing. Reopening a batch clears `ended_by_id`:
it is no longer ended, so nobody ended it. Linking a transfer counts as a
change, even when it is the routine late link.

**Automatic work belongs to whoever triggered it.** Default values a batch
writes at its boundaries are recorded by the person who opened or closed it,
and closing a batch closes its open nested batches in that person's name.

**The services take a user id** (`started_by_id=...`) instead of reading
`current_user()`. They stay usable by any consumer and free of request state,
and importing `identity` from a service would be circular anyway. The cost
is that a tool can forget to pass it, so the tests sign in and check each
write.

**What this does not answer.** Only the latest change is kept, with no time
and no previous value, and a deleted reading or batch leaves no trace. If a
brewery needs a full history -- who changed what, from what, and when --
that is an append-only log alongside these columns, not a replacement for
them; it has not been built. Readings don't record when they were entered,
only `observed_at`.

Tools show authors as the person's name, or their email if they have none.
That means operators see the names and addresses of colleagues who share
their rows, which is the point of attribution and no wider than the rows
themselves; the people tools remain admin-only.

---

## Batch measurements and their window

**Status: the window and batch properties are built; the convention and both
warnings are decided, not built.**

A batch's measurements are event frame attributes, and their wiring is
ELEMENT-scoped, as upstream's is: one tag per element and attribute template,
shared by every batch of that type run on that element. A reading carries no
batch id. What makes a reading "this batch's" is only its time: the batch page
counts readings from `started_at` to `ended_at`, **both ends inclusive**, so
the end value written at close is part of the batch it closes. That choice has
two edges.

### Batches running side by side

On a non-exclusive element such as a brewhouse, two brews overlap and share
the same Gravity tag, so each brew's window takes in the other's readings. An
exclusive element can't overlap itself, so a fermenter is unaffected.

**Convention: a batch on non-exclusive equipment holds properties, not
measurements.** A Brew's measurements live on its steps, because each step
runs on an exclusive child element (one mash in the mash mixer, one boil in
the kettle) whose batches cannot collide. Original Gravity, which feels
brew-wide, is taken at knockout or wort cooling. What really is brew-wide is
what the brew *is*, its brand, and that is a property (see *Batch
properties* below), which no two batches share.

**Follow-up:** warn when someone adds a measurement to a batch type whose
element template is non-exclusive, and point them at a step instead, or at
making it a property if it is a label. A warning rather than a refusal, in
case a brewery has a reason; it belongs in the service's return or the admin
tool's reply so that every consumer sees it.

**Held in reserve:** an optional batch id on `tag_values`, so a batch shows
only readings recorded for it. It is the only option that fully removes the
overlap, but it is a schema change, a departure from upstream, and every
write path (MCP included) would have to say which batch a reading is for.
Worth it only if a brewery needs brew-level *measurements* on overlapping
brews; brand needed a property, not this. A tag per batch was considered and
rejected, as upstream rejected it: tags would multiply with every batch.

### Batch properties

**Status: built.** `is_property` / `is_required` on
`event_frame_attribute_templates`, the `event_frame_properties` table, the
rules in `services/event_frame_properties.py`.

A **property** is what a batch is, as opposed to what was measured during
it. Brand is the one that matters: it is fixed for the batch, picked when the
batch starts, and it is how batches get grouped more than anything else. A
reading belongs to a batch only through its time, and two brews in flight on
one brewhouse share the time, so the earlier brew's window took in the later
brew's brand. A property is kept against the batch itself, one row per batch
and property, so overlapping batches cannot mix theirs up, and "every Lager
batch" is a plain query across brews, fermentations and canning runs. It is
the same idea as an AF event frame attribute with no data reference.

- **The batch type decides.** A property is an attribute template with
  `is_property` set, so a batch type has a brand when it defines one, the
  way it has a gravity. A cleaning defines none; there is no "has a brand"
  switch on the template, and no "None" option on the list, which would
  count as a brand and could be picked for a fermentation.
- **Required where defined.** `is_required` stops a batch of that type
  starting without a value, so a missing brand only ever means the type has
  none. It is checked at the start, so batches older than the setting show
  "not set" rather than refusing anything. Only a property can be required.
- **Fixed kind.** `is_property` is set when the attribute is created and
  never changes, so nothing converts readings into properties. To switch,
  delete and recreate. A property has no tag and no wiring, takes a start
  default at most (a database check refuses an end default), and the start
  default becomes the batch's value rather than a reading.
- **A brand changes where the beer does.** A Lager brew feeds a Dry Hopped
  Lager fermentation; an IPA fermentation feeds a Hazy IPA conditioning.
  Each batch's brand is picked when it starts, so nothing is copied down the
  line and nothing needs keeping in step. Left blank, a property takes its
  start default, then the value its source batch (`source_event_frame_id`)
  holds on the same list, which covers a packaging run drawing from a brite
  tank.
- **The one warning.** A batch whose brand differs from its source is
  normal, so that is never flagged. What is flagged, once, is the batches
  going *into* one batch disagreeing: a Lager brew and an IPA brew knocking
  out into the same fermenter. The check looks one hop upstream (the batch's
  source, and each transfer into it or that transfer's source), which keeps
  yeast out: a yeast pitch delivers into a wort cooling, not into the
  fermentation, so its donor is never compared. Catching one IPA brew going
  into a Dry Hopped Lager fermenter would need the list to know which brands
  are made from which; not built.
- **What is kept.** The value and who set it and last changed it, like a
  reading's attribution, with no time and no history. Deleting a batch takes
  its properties; deleting a property deletes its values behind a preview
  that counts them; a list option a batch holds cannot be deleted, and
  `delete_user` counts property authorship.

### Back-to-back batches

Half-open frames may touch: one batch can end at exactly the moment the next
starts on the same element. A reading at that instant then falls in both
windows. With boundary defaults it is worse than a duplicate: the closing
batch's end value (say, FV Status "Empty") and the next batch's start value
("Ready to fill") are written at the same instant on the same tag, and the
later-written start value becomes the finished batch's latest.

No counting rule gets both cases right -- excluding the end instant would drop
the end value written at close -- and refusing touching batches would break
transfers, which line up end to end on purpose.

**Follow-up:** warn on the web forms (as their other warnings do) when a start, close, reopen or time
edit would make a batch touch another on the same element: the reading at that
moment counts for both, so leave a minute between them if that matters. This
is web-only and changes no rule.

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
  things gated it. **Identity:** forms make writing easy and high-volume,
  which needed sign-in and attribution; both now exist (*Identity and access*
  above). **Distribution:** a page
  declaring `mcp` cannot be shared publicly and each viewer must add the
  connector themselves, so every brewer authors their own page rather than
  the cellar sharing one URL. Worth doing first either way: the MCP tools
  declare no `annotations`, and adding `readOnlyHint` would let read tools
  cache and coalesce while making write tools structurally un-cacheable and
  un-watchable.
