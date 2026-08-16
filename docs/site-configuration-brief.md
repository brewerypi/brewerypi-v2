# Brewery Pi: site configuration brief

A fill-in-the-blanks brief for setting up one site. Fill in what you know,
leave the rest blank, and paste it into a chat with the Brewery Pi admin
connector. The agent asks about the gaps rather than guessing at them.

It is built around batches, because a batch is what a brewery already
writes down: a brew, a fermentation, a canning run, a few values at the
start and a few at the end. Readings taken on rounds rather than per batch,
such as boiler pH or glycol temperature, have their own section at the end.

## How to use it

There are two ways to fill this in. Both end up in the same place, so pick
whichever you would rather work in.

- **In a spreadsheet.** Ask the agent for a spreadsheet version of this
  brief, fill it in, and upload it to the chat. It has one tab per
  section below, and carries no instructions of its own, so keep this
  page open beside it.
- **In this page.** If you are comfortable editing Markdown, fill in the
  blanks below, then copy everything from the **Part 1: About the
  company** heading to the end of the file and paste it into the chat.
  Nothing above that heading should go with it. The tables are the fiddly
  part: every row needs the same number of pipes as its header row, so
  take the spreadsheet if that sounds like a nuisance.

Either way:

- **One site at a time.** Part 1 is company-wide and only needs answering
  once. Part 2 describes a single site; fill it in again for each further
  site, since no two sites share their equipment definitions.
- **Blanks are fine.** A blank is a question the agent will ask you. A
  guess is a mistake that gets built.
- **Answer in your own words.** Do not translate anything into system
  terms. That mapping is the agent's job, and it is the part most worth
  testing.

## A worked example

The whole brief, answered for a mid-size ale house. It is here to show the
shape of an answer, not to be copied: your equipment, batches and lists
will all differ. Notice that everything referred to further down gets
defined further up, which is what makes the tables join.

### The company, answered

- Company name: Example Brewing Co.
- Short name or abbreviation for it: EBC

Lists. Nothing here shares a list unless the options really are the same:
a brite tank does not pass through a fermenter's states, and a keg size is
not a can size.

| List | Options |
| --- | --- |
| FV Status | Empty, Clean, Filling, Fermenting, Crashing, Ready to dump |
| BBT Status | Empty, Clean, Filling, Carbonating, Ready to package, Packaging |
| Brand | Pale Ale, IPA, Pilsner, Stout |
| Can format | 12 oz, 16 oz, 19.2 oz |
| Keg format | 1/2 bbl., 1/6 bbl. |
| Yes/No | Yes, No |

### The site, answered

- The company this site belongs to: Example Brewing Co.
- Site name: Springfield
- Short name or abbreviation for it: SPR
- Town or city it is in, with the state or country: Springfield, Illinois

Equipment. The kind is the type of vessel; what they are called is the
name painted on each individual one, not the kind said again. Leave "Part
of" blank unless a piece sits inside a bigger one. Note the boilers:
everything on site goes in here, not just the vessels that run batches.

| Kind of equipment | What they are called | Where they live | Part of |
| --- | --- | --- | --- |
| Brewhouse | BH1 | Brewhouse | |
| Mash mixer | MM1 | Brewhouse | BH1 |
| Lauter tun | LT1 | Brewhouse | BH1 |
| Kettle | BK1 | Brewhouse | BH1 |
| Whirlpool | WP1 | Brewhouse | BH1 |
| Fermenter | FV01-FV12 | Cellar | |
| Brite tank | BBT01-BBT04 | Cellar | |
| Canning line | CL1 | Packaging | |
| Kegging line | KL1 | Packaging | |
| Boiler | B1, B2 | Utilities | |

What a batch is called on each kind of equipment, and whether more than one
can run on a single piece at a time. Not every kind runs a batch: the
boilers here are only ever read on rounds. Only the brewhouse takes more
than one, because two brews are often in flight at once.

| Kind of equipment | What a batch is called | More than one at once? |
| --- | --- | --- |
| Brewhouse | Brew | Yes |
| Mash mixer | Mashing | No |
| Lauter tun | Lautering | No |
| Kettle | Boiling | No |
| Whirlpool | Whirlpooling | No |
| Fermenter | Fermentation | No |
| Brite tank | Conditioning | No |
| Canning line | Canning | No |
| Kegging line | Kegging | No |

Which batches happen inside another batch, and the order they run in.
Mashing is the first step of a brew, lautering the second.

| Equipment | This batch | Happens inside | Order |
| --- | --- | --- | --- |
| Mash mixer | Mashing | Brew | 1 |
| Lauter tun | Lautering | Brew | 2 |
| Kettle | Boiling | Brew | 3 |
| Whirlpool | Whirlpooling | Brew | 4 |

What gets recorded on a batch. "At start" and "at end" are the values
filled in for you by default. Leave them blank when the value differs
every batch and you will type it each time.

| Equipment | Batch | What you record | Units or list | At start | At end |
| --- | --- | --- | --- | --- | --- |
| Brewhouse | Brew | Brand | Brand | | |
| Mash mixer | Mashing | pH | pH | | |
| Mash mixer | Mashing | Conversion complete | Yes/No | | |
| Lauter tun | Lautering | Gravity | °P | | |
| Lauter tun | Lautering | pH | pH | | |
| Kettle | Boiling | Gravity | °P | | |
| Kettle | Boiling | pH | pH | | |
| Whirlpool | Whirlpooling | Gravity | °P | | |
| Whirlpool | Whirlpooling | pH | pH | | |
| Fermenter | Fermentation | Status | FV Status | Filling | Empty |
| Fermenter | Fermentation | Gravity | °P | | |
| Fermenter | Fermentation | Temperature | °F | | |
| Brite tank | Conditioning | Status | BBT Status | Filling | Empty |
| Brite tank | Conditioning | Pressure | psi | | |
| Canning line | Canning | Format | Can format | | |
| Canning line | Canning | TPO | ppb | | |
| Kegging line | Kegging | Format | Keg format | | |
| Kegging line | Kegging | TPO | ppb | | |

What gets read on rounds instead of per batch.

| Kind of equipment | What you read | Units or list |
| --- | --- | --- |
| Boiler | Conductivity | µS/cm |
| Boiler | pH | pH |

---

**⬇  Everything below goes in the chat. Everything above it does not.  ⬇**

---

## Part 1: Your company

Answer once, ever. These are shared by every site.

- Company name:
- Short name or abbreviation for it:

Anywhere you pick from a fixed set of words instead of typing a number:
vessel status, brand, yeast strain, package format. Name the list and give
its options:

| List | Options |
| --- | --- |
|  |  |

Units of measurement are not asked for. A standard set is created with the
company, and anything missing is added the first time you use it further
down, so just write the unit you want beside each measurement.

## Part 2: About this site

Answer once per site. Use a fresh copy for each further site.

- The company this site belongs to:
- Site name:
- Short name or abbreviation for it:
- Town or city it is in, with the state or country:

### The equipment

Everything on site, including equipment that never runs a batch. "What
they are called" is the name on each individual one, like FV01 or BH1,
rather than the kind said again. "Where they live" is the part of the site
a piece sits in: brewhouse, cellar, packaging, utilities. Those answers
are what your data gets grouped under, so there is nothing separate to
fill in for them:

| Kind of equipment | What they are called | Where they live | Part of |
| --- | --- | --- | --- |
|  |  |  |  |

### The batches

What a batch is called on each kind of equipment, and whether more than one
can run on a single piece at a time. A brewhouse usually can, with several
brews in flight at once; a fermenter usually cannot. That second column is
worth getting right, so say so if you are unsure:

| Kind of equipment | What a batch is called | More than one at once? |
| --- | --- | --- |
|  |  |  |

Does a batch on one piece of equipment happen inside a batch on another?
Mashing, on the mash mixer, happens inside a brew on the brewhouse.
Number them in the order they actually run, since nothing else says
which step comes first:

| Equipment | This batch | Happens inside | Order |
| --- | --- | --- | --- |
|  |  |  |  |

### What you record on a batch

One row per thing you write down. Leave the start and end columns blank
when the value differs every batch:

| Equipment | Batch | What you record | Units or list | At start | At end |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

### What you read on rounds

For equipment listed above that you check on a walk-around rather than per
batch: boilers, glycol, water treatment, CIP. Skip it if you have none:

| Kind of equipment | What you read | Units or list |
| --- | --- | --- |
|  |  |  |
