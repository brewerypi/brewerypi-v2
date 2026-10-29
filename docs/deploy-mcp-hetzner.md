# Deploying the BreweryPi MCP server (Hetzner + Caddy)

A step-by-step guide to running the BreweryPi MCP server on a small Hetzner
VPS, behind HTTPS, so brewery staff can add it to Claude as a custom
connector and sign in as themselves.

**Architecture.** Two MCP processes run as `systemd` services bound to
localhost (never exposed directly): the **operator** tier on `127.0.0.1:8000`
and the **admin** tier on `127.0.0.1:8001`. Caddy listens on 443, terminates
HTTPS with automatic Let's Encrypt certificates, and proxies one hostname to
each tier -- `operator.mcp.brewerypi.com` and `admin.mcp.brewerypi.com`.
(One hostname per tier, not a path prefix: OAuth discovery lives under
`/.well-known/` at the host root, so two tiers cannot share a host. Both are
named for their tier so that a connector's URL says which one it is -- a
connector's display name in Claude says nothing about where it points.)

**Sign-in.** Staff sign in through **WorkOS AuthKit** (Google, Microsoft, or
an emailed magic link). AuthKit proves who someone is; the server then checks
them against its own **user list** and refuses everyone else, so signing up
with AuthKit grants nothing. Users and their roles are managed with the
`brewerypi-users` command on the server (step 8). The design is in
`docs/design-decisions.md` under *Identity and access*.

> **Not yet scoped per company.** Sign-in and the user list are enforced, but
> every member currently sees every enterprise on the server. Host one
> company per server until scoping lands.

Throughout, replace `operator.mcp.brewerypi.com` /
`admin.mcp.brewerypi.com` with your hostnames.

---

## 1. Create the server

1. Sign up at <https://www.hetzner.com/cloud> and create a project.
2. Add your SSH public key under **Security → SSH Keys**. On Windows, if you
   don't have a key yet, run `ssh-keygen -t ed25519` in PowerShell and paste
   the contents of `C:\Users\<you>\.ssh\id_ed25519.pub`.
3. Create a server: **Debian 13** ("Trixie"). For the lowest cost, choose an
   **EU location** — **Nuremberg** or **Helsinki** — and the **Cost-Optimized
   CX23** plan (2 vCPU / 4 GB / 40 GB, x86, ~$6.49/mo). The Cost-Optimized
   tier is EU-only; a US location (Ashburn/Hillsboro) only offers the pricier
   **Regular Performance (CPX)** tab (~$22.99/mo for the smallest), worth it
   only if your demo users need US-local latency. In the **Networking**
   section, make sure **Public IPv4** is ticked — it's a ~$0.60/month add-on
   that is *not* enabled by default, and you need it for the DNS A record and
   SSH. Attach your SSH key. Create it, and note the public **IPv4** address.

## 2. Point the subdomains at it

In the DNS settings for your domain, add two **A records**:

| Type | Name         | Value             |
| ---- | ------------ | ----------------- |
| A    | operator.mcp | `<server-ipv4>`   |
| A    | admin.mcp    | `<server-ipv4>`   |

If your DNS is on Cloudflare, set these records to **DNS only** (grey cloud)
so the certificate challenge is straightforward. Wait a couple of minutes,
then confirm from your laptop:

```
nslookup operator.mcp.brewerypi.com
nslookup admin.mcp.brewerypi.com
```

## 3. First login and basics

```
ssh root@<server-ipv4>
apt update && apt upgrade -y
apt install -y python3-venv python3-pip git ufw curl
ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw --force enable
```

## 4. Get the code and install it

```
git clone https://github.com/brewerypi/brewerypi-v2.git /opt/brewerypi
cd /opt/brewerypi
python3 -m venv .venv
.venv/bin/pip install -e ".[mcp,migrations]"
```

**Adopt Alembic on the existing database (one time).** The current `app.db`
was built by `create_all`, so tell Alembic it is already at the latest
migration — this stamps it without re-running, so nothing is rebuilt:

```
cd /opt/brewerypi
DATABASE_URL=sqlite:////opt/brewerypi/app.db .venv/bin/alembic stamp head
```

From now on, schema changes ship as migrations and you apply them with
`alembic upgrade head` (see "Updating later"). On a brand-new box with no
`app.db` yet, run `alembic upgrade head` instead of stamping.

## 5. Create and seed the database

```
cd /opt/brewerypi
.venv/bin/brewerypi                              # creates app.db
.venv/bin/python scripts/seed_sample_data.py     # loads sample data
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('app.db'); \
print(c.execute('PRAGMA journal_mode=WAL').fetchone()); c.close()"
```

The last line switches SQLite into WAL mode for smoother concurrent reads.

## 6. Set up WorkOS AuthKit

WorkOS is free for this (AuthKit costs nothing up to 1 million monthly
active users; paid extras such as enterprise SAML or a custom sign-in domain
are not needed). Brewery staff never deal with WorkOS directly -- they only
see its sign-in page. Dashboard labels move around; if one below doesn't
match, WorkOS's MCP guide is at <https://workos.com/docs/authkit/mcp>.

1. Sign up at <https://workos.com> and open the **Production** environment
   (Staging works for a trial, with WorkOS's shared Google/Microsoft
   credentials).
2. **Authentication**: turn on **Email + Magic Auth**, **Google OAuth** and
   **Microsoft OAuth**. In Production, Google and Microsoft each need your
   own OAuth client -- the dashboard walks through creating one.
3. **Applications -> Configuration**: enable **Dynamic Client
   Registration**. This is what lets Claude register itself as a client when
   someone adds the connector.
4. Note the **AuthKit domain** (e.g. `https://your-app.authkit.app`).
5. **Resource indicators**: add both tiers' URLs exactly as the server will
   advertise them -- `https://operator.mcp.brewerypi.com/mcp` (make this
   the default, since most people use it) and
   `https://admin.mcp.brewerypi.com/mcp`. Tokens are minted for one of these
   and the server rejects a token minted for anything else.
6. **API Keys**: copy the **secret key** (`sk_...`). The server uses it only
   to look up a person's email the first time they sign in.

## 7. Run the MCP servers as services

Keep the secrets out of the unit files, in one root-only file:

```
mkdir -p /etc/brewerypi
cat > /etc/brewerypi/mcp.env <<'EOF'
DATABASE_URL=sqlite:////opt/brewerypi/app.db
AUTHKIT_DOMAIN=https://your-app.authkit.app
WORKOS_API_KEY=sk_live_REPLACE_ME
EOF
chmod 600 /etc/brewerypi/mcp.env
```

Note the **four** slashes in the SQLite URL -- that is `sqlite://` plus the
absolute path `/opt/brewerypi/app.db`.

Create `/etc/systemd/system/brewerypi-mcp-operator.service` (the operator
tier). Each unit is named for its tier, like the hostnames:

```ini
[Unit]
Description=BreweryPi MCP server (operator)
After=network.target

[Service]
WorkingDirectory=/opt/brewerypi
EnvironmentFile=/etc/brewerypi/mcp.env
Environment=MCP_ROLE=operator
Environment=MCP_HOST=127.0.0.1
Environment=MCP_PORT=8000
Environment=MCP_PATH=/mcp
Environment=MCP_BASE_URL=https://operator.mcp.brewerypi.com
ExecStart=/opt/brewerypi/.venv/bin/brewerypi-mcp
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
```

and `/etc/systemd/system/brewerypi-mcp-admin.service`, identical except:

```ini
Description=BreweryPi MCP server (admin)
Environment=MCP_ROLE=admin
Environment=MCP_PORT=8001
Environment=MCP_BASE_URL=https://admin.mcp.brewerypi.com
```

`MCP_BASE_URL` is each tier's **public** address; it must match the resource
indicator from step 6. Then:

```
systemctl daemon-reload
systemctl enable --now brewerypi-mcp-operator brewerypi-mcp-admin
systemctl status brewerypi-mcp-operator brewerypi-mcp-admin   # both active (running)
```

A service that exits at once with *"sign-in is not configured"* is missing a
setting from the env file. That refusal is deliberate: the server will not
start open by accident. (`MCP_AUTH=none` disables sign-in entirely, for
local development only -- never set it here.)

## 8. Add the first users

The admin tools need an admin to exist, so the first account is created
here, on the server (the **bootstrap**). `--superuser` is server-wide access,
including creating enterprises:

```
cd /opt/brewerypi
set -a; . /etc/brewerypi/mcp.env; set +a
.venv/bin/brewerypi-users add you@yourbrewery.com --name "Your Name" --superuser
```

Then everyone else -- by the email they will sign in with -- and their role
in an enterprise (by id or abbreviation). No `--site` means every site,
including ones added later; `--site` (repeatable) restricts them:

```
.venv/bin/brewerypi-users add brewer@yourbrewery.com
.venv/bin/brewerypi-users grant brewer@yourbrewery.com BRW --role operator
.venv/bin/brewerypi-users add cellar@yourbrewery.com
.venv/bin/brewerypi-users grant cellar@yourbrewery.com BRW --role operator --site ATL
.venv/bin/brewerypi-users list
```

Someone who has left: `update their@email --no-active` (keeps the record,
blocks sign-in). Someone whose sign-in account was deleted and recreated:
`reset-sign-in their@email`. A remote user who wants times in their own
clock: `update their@email --timezone America/Denver`. Run
`brewerypi-users --help` for the rest.

## 9. Install Caddy and configure HTTPS

```
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

(If that method has changed, see <https://caddyserver.com/docs/install>.)

Replace `/etc/caddy/Caddyfile` with:

```
operator.mcp.brewerypi.com {
    reverse_proxy 127.0.0.1:8000
}

admin.mcp.brewerypi.com {
    reverse_proxy 127.0.0.1:8001
}
```

No secret paths any more -- sign-in is the credential. Reload Caddy; it will
obtain both certificates automatically:

```
systemctl reload caddy
journalctl -u caddy --no-pager | tail -20   # watch for certificate success
```

## 10. Test before handing it out

Run these **on the server** (it has a real `curl`). An unauthenticated call
must be refused with `401` and point at the sign-in metadata:

```
curl -s -i -X POST https://operator.mcp.brewerypi.com/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -5
```

Expect `HTTP/2 401` and a `www-authenticate: Bearer resource_metadata=...`
header. Then the metadata itself should name your AuthKit domain:

```
curl -s https://operator.mcp.brewerypi.com/.well-known/oauth-protected-resource/mcp
```

Repeat both for `admin.mcp.brewerypi.com`. If the public URL fails, test the
service directly (bypassing Caddy) -- `http://127.0.0.1:8000/mcp` returning
`401` means the service is fine and the issue is in Caddy or DNS.

> **Windows note:** in PowerShell `curl` is an alias for `Invoke-WebRequest`
> with different syntax, so run the tests from the server.

## 11. Add it to Claude (each user)

Each person adds the connector themselves and signs in as themselves:

- Operators: `https://operator.mcp.brewerypi.com/mcp`
- Admins, additionally: `https://admin.mcp.brewerypi.com/mcp`

On **Pro or Max**: Customize -> Connectors -> **+** -> **Add custom
connector** -> paste the URL -> **Add** -> **Connect**, which opens the
AuthKit sign-in page. On **Team/Enterprise**, an Owner adds it once under
Organization settings -> Connectors, and members connect (and sign in)
individually.

Whichever connector someone uses, their **role** decides what they may do:
the admin tier refuses anyone who is not an admin, and someone not on the
user list is refused on every call with a message telling them to ask an
administrator. Admins will usually enable the lighter operator connector for
day-to-day work and the admin one only when changing configuration.

Try: *"Browse the brewery hierarchy,"* or *"What tags are in the Brewhouse,
and what are the latest Mash Temp readings?"*

If a connector its user thinks of as the operator one refuses with *"… is
not an administrator; use the operator connector"*, it was added with the
admin URL: only the admin tier says that. Remove it and add the operator URL.

## Moving a shared-secret deployment to sign-in

For a box set up from an earlier version of this guide (secret paths on
`mcp.brewerypi.com`, no sign-in). Do it once the sign-in release is on
`main`. The order matters in one place: the old `mcp` hostname keeps
serving the old connectors until everyone has moved, so its DNS record is
removed **last**.

### Optional: test side by side first

To try sign-in without disturbing the old connectors, run the new version
from a second checkout (say `/opt/brewerypi-next`) against a **copy** of the
database, as two extra services (`brewerypi-next-mcp-operator` on 8010,
`brewerypi-next-mcp-admin` on 8011) with their own env file,
using the WorkOS **Staging** environment. In Caddy, add `operator.mcp` ->
8010 and `admin.mcp` -> 8011 as new site blocks and leave the
`mcp.brewerypi.com` block exactly as it is. Nothing the old connectors use
changes, and backing out is stopping the two services and removing the two
site blocks.

The test's users do not carry over: they live in the copied database, and
they are linked to **Staging** identities. Production is a separate WorkOS
environment that gives every person a new id, so a user linked in Staging
would be refused in Production as "already linked to a different sign-in"
(`brewerypi-users reset-sign-in` fixes one such user).

### The cutover

1. **Before you start:** the `operator.mcp` and `admin.mcp` DNS records
   exist (step 2). **Keep the `mcp` record** -- the old connectors still use
   it.
2. **WorkOS Production** (step 6): enable the same sign-in methods, with
   your own Google and Microsoft OAuth clients; enable Dynamic Client
   Registration; add both resource indicators with `operator.mcp` as the
   default; note the Production AuthKit domain and the `sk_live_...` key.
3. **Back up, pull and migrate** the live database:
   ```
   cp /opt/brewerypi/app.db /root/app.db.before-sign-in
   cd /opt/brewerypi && git pull
   .venv/bin/pip install -e ".[mcp,migrations]"
   DATABASE_URL=sqlite:////opt/brewerypi/app.db .venv/bin/alembic upgrade head
   ```
   From here the old services **refuse to start** until sign-in is
   configured, so carry straight on.
4. **Settings and services** (step 7): create `/etc/brewerypi/mcp.env` with
   the **Production** values. The operator unit gets its tier in its name,
   so retire the old one first:
   ```
   systemctl disable --now brewerypi-mcp
   rm /etc/systemd/system/brewerypi-mcp.service
   ```
   Then create `brewerypi-mcp-operator.service` and rewrite
   `brewerypi-mcp-admin.service` (ports 8000/8001, `MCP_BASE_URL`
   `operator.mcp` / `admin.mcp`), run `systemctl daemon-reload`, and
   `systemctl enable --now brewerypi-mcp-operator`, then
   `systemctl restart brewerypi-mcp-admin`.
5. **Users** (step 8): add yourself as superuser, then everyone else with
   their roles. This is a fresh list in the live database.
6. **Caddy** (step 9): add the `operator.mcp` -> 8000 and `admin.mcp` ->
   8001 site blocks (or repoint them from 8010/8011 if you tested side by
   side), and **delete the whole `mcp.brewerypi.com` block**. `caddy validate
   --config /etc/caddy/Caddyfile && systemctl reload caddy`. The old secret
   URLs stop working at this point.
7. **Test** (step 10) on both hostnames.
8. **Tell everyone to move:** remove the old connector in Claude and add
   the new URL (step 11) -- connector URLs can't be edited in place.
9. **If you tested side by side, tear it down:**
   ```
   systemctl disable --now brewerypi-next-mcp-operator brewerypi-next-mcp-admin
   rm /etc/systemd/system/brewerypi-next-mcp*.service /etc/brewerypi/next.env
   systemctl daemon-reload
   rm -rf /opt/brewerypi-next
   ```
10. **Last: delete the `mcp` A record** at your DNS provider, once nobody
    is still using an old connector. Nothing on the server answers for it
    after step 6, so this only tidies DNS -- but deleting it earlier would
    have cut the old connectors off mid-move.

## Updating later

```
cd /opt/brewerypi
git pull
.venv/bin/pip install -e ".[mcp,migrations]"
DATABASE_URL=sqlite:////opt/brewerypi/app.db .venv/bin/alembic upgrade head
systemctl restart brewerypi-mcp-operator brewerypi-mcp-admin
```

The `alembic upgrade head` applies any new migrations; it's a no-op when
there are none, so it's safe to run every time. Back up `app.db` first (see
Notes) if the release touches data rather than just code.

### Backfilling defaults an existing company missed

Some config arrives with `create_enterprise` and so never reaches a company
created before it existed — the default measurement units are the first
case. Ask the **admin** connector to add the standard brewing units to the
company, or do it on the box:

```
cd /opt/brewerypi
DATABASE_URL=sqlite:////opt/brewerypi/app.db .venv/bin/python - <<'PY'
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from brewerypi import services
from brewerypi.config import DATABASE_URL

engine = create_engine(DATABASE_URL)
with Session(engine) as session:
    for ent in services.list_enterprises(session):
        added = services.add_default_measurement_units(session, ent.id)
        print(f"{ent.name}: added {len(added)}")
    session.commit()
PY
```

It skips any unit whose symbol or name the company already uses rather than
failing, and renames nothing, so a second run adds 0 and is harmless.

## Notes

- **Revoking access:** `brewerypi-users update their@email --no-active`
  takes effect on their next tool call; no URL needs to change.
- **Write access:** operators can record and correct readings and run
  batches; admins can also change configuration. Who did what is not yet
  recorded (attribution is a later phase of *Identity and access*).
- **Resetting the demo data:** to wipe accumulated writes, rebuild the DB —
  `systemctl stop brewerypi-mcp-operator brewerypi-mcp-admin`,
  `rm -f app.db app.db-wal app.db-shm`, `.venv/bin/brewerypi`,
  `.venv/bin/python scripts/seed_sample_data.py`,
  `systemctl start brewerypi-mcp-operator brewerypi-mcp-admin`. The user
  list lives in the same file, so
  re-add the users (step 8) before anyone signs in again.
- **Backups:** the whole database is the single file `/opt/brewerypi/app.db`;
  copy it off the box to back it up.
