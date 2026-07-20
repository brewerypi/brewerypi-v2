# House context

The agent reads your brewery's structure straight from the database -- sites,
areas, equipment, measurements, batch types. What it cannot read is how your
people *talk* and the rules that live in their heads. That is house context.

House context is stored **on the enterprise, in the database**. It is written
once and every user inherits it: operators, other admins, anyone who connects,
on any plan, with no per-person setup. Nobody edits a file.

## How it normally gets there

At the end of setting a brewery up, the admin agent offers to save it, using
the words the user already used while configuring ("FVs", "the 22",
"barrels"), plus a couple of follow-ups about operating ranges and house
rules. If you took that offer, you are done.

## Adding or changing it later

Ask an admin chat to do it. There is no form and no file:

> "Remember that we call fermenters FVs and the 22-barrel brewhouse is 'the
> 22'. Ales usually ferment at 66 to 70. We don't repitch yeast past
> generation 8."

The agent will show you what is currently stored, then save the replacement
once you confirm. It replaces rather than appends, so ask it to keep what is
already there if you only mean to add something.

To review it from any chat: *"what's our house context?"*

## Doing it thoroughly

If you would rather be interviewed than dictate, paste this into an admin
chat:

```
Interview me to write our house context, then save it.

First browse our setup -- sites, areas, equipment kinds, the equipment
itself, measurements, batch types, lists -- and use what you find to ask
sharper questions. Do NOT ask me anything you can already see: not our
sites, not how many fermenters we have, not their names, not our units. If
the system is empty, tell me, and we should configure the brewery first --
this goes much better afterwards.

Then ask me two or three questions at a time, conversationally, about:

1. Vocabulary. What we actually call things day to day, including short
   forms and words to avoid. If you saw equipment names, ask what we call
   those out loud.
2. Ambiguity. Where you should stop and ask rather than guess. If you saw
   two of anything -- brewhouses, sites, packaging lines -- ask how we tell
   them apart in speech.
3. What normal looks like. Rough ranges: fermentation temperatures, typical
   original and terminal gravity, how long a fermentation runs, target
   packaged oxygen. Approximate is fine; these are context, not limits.
4. House rules and gotchas. Limits we hold to, things that have bitten us,
   anything you should flag when you see it.
5. How we want you to behave. How much to volunteer beyond the answer, and
   what should always be confirmed with a person.

If I say "skip", move on -- short and accurate beats long and guessed.

When we are done, show me what you propose to save, keep it under about 400
words, include only what I actually told you, and do not restate anything
you can read from the database. Then save it with set_house_context.
```

## What belongs in it

Only what cannot be looked up:

- **Vocabulary** -- "we say FV, not fermenter"
- **Ambiguities** -- "Atlanta has two brewhouses; always ask which"
- **What normal looks like** -- rough operating ranges
- **House rules** -- "no repitching past generation 8"
- **Behavior preferences** -- what to flag, what to confirm

Not equipment lists, site names, measurements or units. Those are already in
the system, and duplicating them here means two sources that can disagree.

It is capped at 4,000 characters, because it is read into conversations. Aim
for about 400 words.

## Notes for maintainers

While the vocabulary layer is still being tuned, log what the agent got wrong
with the **exact words** used. Three kinds matter, each with a different fix:

1. **Jargon** -- said "element attribute" instead of "measurement". Fix in
   `_SHARED_INSTRUCTIONS` (`mcp_server.py`).
2. **Misread the request** -- wrong measurement for "mash temp". Fix in that
   tool's description.
3. **Wrong tool**, or asked something it could have looked up. Fix in the
   tool description or an ambiguity rule.

| What I said | What it did | What I wanted |
| --- | --- | --- |
|  |  |  |

Anything house-specific belongs in house context; anything true of every
brewery should be promoted into the server instructions. Target ranges move
to recipe specifications when those land (see `docs/design-decisions.md`).
