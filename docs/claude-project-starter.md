# Brewery Pi — Claude Project instructions (starter)

House context for the Brewery Pi connector: the words your brewery uses, how
you make what you make, and what normal looks like. None of it can be looked
up, so the agent only knows what you tell it here.

## Where this goes

Paste it into your Claude Project's **custom instructions**, alongside the
Brewery Pi connector — not into the Project's uploaded files. Custom
instructions sit in front of the agent on every message; an uploaded file is
only pulled in when it looks relevant, and house vocabulary has to apply to
the messages that do not look like they need a glossary.

Copy everything from the **How we talk here** heading to the end of the file.
Nothing above that heading belongs in the box.

## How to edit it

Edit it in the custom-instructions box itself. Changes take effect on your
very next message — no commit, no deploy, no connector refresh — so that is
the right place to tune wording while you are testing, and it leaves no
second copy to keep in step.

- Avoid Word and Google Docs. Both rewrite what you type: dashes become
  bullet glyphs, `1.` becomes a managed list, quotes curl. What you copy back
  out is no longer what you edited. Use the box, or Notepad if you need a
  file to hand someone.
- You do not have to write it yourself. Tell Claude in chat that you call
  brite tanks BBTs and do not repitch past generation 8, and ask it to
  rewrite the section for you to paste.

## What belongs in it

Only what cannot be looked up. Your sites, vessels, measurements and units
are read straight out of Brewery Pi once it is configured, so listing them
here only gives the agent a second answer that goes stale. Everything below
is a starting draft — delete anything that does not fit.

What each section is for:

- **How we talk here** — every word the agent got wrong. Add to it whenever
  it uses a term you would not.
- **Our production process** — a rough outline so it can reason about what
  happens when. One block per product line.
- **What normal looks like** — the highest-value thing you can add. Rough
  ranges are fine. Fill a line in or delete it: a blank tells the agent
  nothing and invites it to guess. Only the fermentation temperatures are
  beer-specific; the rest apply to anything you package. Where one number
  will not cover the house, qualify the line and add another, the way ale
  and lager already do.
- **Things to watch for** — house rules, and anything that has bitten you.
- **How I want you to behave** — your working style, not the house's.

This file is yours alone; it is not shared with the rest of the brewery. The
vocabulary in it is the house's, though, so if a teammate starts their own
Project, send them your copy rather than let them start from scratch.

## Other product lines

The block below uses beer as its worked example. If you run other lines, add
a block for each and delete what you do not make. Rough shapes to start from:

**Hard seltzer** — dissolve sugar to gravity, with no mash, lauter or boil;
pitch with nutrient and ferment dry; carbon-filter the base to strip flavor
and color; blend flavor and acid to spec in a BBT; carbonate; package.

**RTD** — blend spirit or base, flavor, sweetener and water to spec;
carbonate if the format calls for it, with no fermentation; package.

## Notes for testing

Keep a running list of anything the agent got wrong, with the **exact words**
you used. Three kinds are worth capturing, because each has a different fix:

1. **It used jargon** — said "element attribute" instead of "measurement".
   Fix goes in the server instructions.
2. **It misread you** — you asked for the mash temp and it picked the wrong
   measurement. Fix goes in that tool's description.
3. **It used the wrong tool** — or asked you something it could have looked
   up. Fix goes in the tool description, or in an ambiguity rule.

| What I said | What it did | What I wanted |
| --- | --- | --- |
|  |  |  |

Once something here has proven itself, tell me and I will promote it into the
server instructions so every connector inherits it automatically.

---

**⬇  Everything below goes in the box. Everything above it does not.  ⬇**

---

## How we talk here

- We call a brewhouse batch a **brew** (not a "turn").
- We call a fermenter an **FV**.
- We call a brite tank a **BBT**.
- Knockout is usually shortened to **KO**.
- A diacetyl rest is usually a **D-rest**.
- Packaged oxygen is **TPO** — total package oxygen, headspace included, not
  dissolved oxygen alone.

## Our production process, in order

### Beer

1. Mill and mash in the mash mixer.
2. Lauter, then boil in the kettle with hop additions.
3. Whirlpool, then knock out through the heat exchanger into an FV.
4. Pitch yeast; primary fermentation.
5. Diacetyl rest near terminal gravity, confirmed by VDK check.
6. Crash cool; harvest yeast from the cone.
7. Centrifuge or filter into a BBT for conditioning and carbonation.
8. Package: canning, bottling or kegging run.

Say which line a batch belongs to when it is not obvious from the equipment.

## What normal looks like

- Typical OG range:
- Ale fermentation temperature range:
- Lager fermentation temperature range:
- Typical terminal gravity range:
- Typical fermentation duration range:
- Typical pH range at crash cool:
- Target TPO range:

The system does not hold per-brand specifications, so treat these as context
for conversation, not as pass/fail limits. Say when a number looks unusual
against them, but leave the judgement to the user.

## Things to watch for

- During fermentation, gravity that stalls for more than a day is worth
  flagging.
- Temperature climbing during a crash usually means a glycol problem.
- Yeast beyond generation 8 is not repitched.

## How I want you to behave

- Answer the question first, then add anything notable in one sentence.
- Ask which vessel or which batch when it is ambiguous.
- Always confirm before recording, changing or deleting anything.
- Keep it brief for data entry; go deeper for analysis.
- Report the data confidently; leave process decisions to the user.
