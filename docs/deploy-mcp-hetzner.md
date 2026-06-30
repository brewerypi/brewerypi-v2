# Deploying the BreweryPi MCP server (Hetzner + Caddy)

A step-by-step guide to running the read-only MCP server on a small Hetzner
VPS, behind HTTPS, so a few demo users can add it to Claude as a custom
connector.

**Architecture.** The MCP server runs as a `systemd` service bound to
`127.0.0.1:8000` (never exposed directly). Caddy listens on 443, terminates
HTTPS for `mcp.brewerypi.com` with an automatic Let's Encrypt certificate,
and reverse-proxies a **secret path** to the local server. The secret path
is the access credential: only people who have the full URL can reach the
tools, and every tool is read-only.

Throughout, replace `mcp.brewerypi.com` with your subdomain and
`REPLACE_WITH_SECRET` with a random token you generate in step 6.

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
   only if your demo users need US-local latency. Attach your SSH key. Create
   it, and note the public **IPv4** address.

## 2. Point the subdomain at it

In the DNS settings for your domain, add an **A record**:

| Type | Name | Value             |
| ---- | ---- | ----------------- |
| A    | mcp  | `<server-ipv4>`   |

If your DNS is on Cloudflare, set this record to **DNS only** (grey cloud) so
the certificate challenge is straightforward. Wait a couple of minutes, then
confirm from your laptop:

```
nslookup mcp.brewerypi.com
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
.venv/bin/pip install -e ".[mcp]"
```

## 5. Create and seed the database

```
cd /opt/brewerypi
.venv/bin/brewerypi                              # creates app.db
.venv/bin/python scripts/seed_sample_data.py     # loads sample data
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('app.db'); \
print(c.execute('PRAGMA journal_mode=WAL').fetchone()); c.close()"
```

The last line switches SQLite into WAL mode for smoother concurrent reads.

## 6. Run the MCP server as a service

Generate a secret token and keep it handy:

```
openssl rand -hex 16
```

Create `/etc/systemd/system/brewerypi-mcp.service`:

```ini
[Unit]
Description=BreweryPi MCP server
After=network.target

[Service]
WorkingDirectory=/opt/brewerypi
Environment=MCP_HOST=127.0.0.1
Environment=MCP_PORT=8000
Environment=MCP_PATH=/mcp
Environment=DATABASE_URL=sqlite:////opt/brewerypi/app.db
ExecStart=/opt/brewerypi/.venv/bin/brewerypi-mcp
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
```

Note the **four** slashes in the SQLite URL — that is `sqlite://` plus the
absolute path `/opt/brewerypi/app.db`. Then:

```
systemctl daemon-reload
systemctl enable --now brewerypi-mcp
systemctl status brewerypi-mcp        # should show active (running)
```

## 7. Install Caddy and configure HTTPS + the secret path

```
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

(If that method has changed, see <https://caddyserver.com/docs/install>.)

Replace `/etc/caddy/Caddyfile` with this, substituting your subdomain and the
secret from step 6:

```
mcp.brewerypi.com {
    @mcp path /REPLACE_WITH_SECRET/*
    handle @mcp {
        uri strip_prefix /REPLACE_WITH_SECRET
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        respond "Not found" 404
    }
}
```

Reload Caddy; it will obtain the certificate automatically:

```
systemctl reload caddy
journalctl -u caddy --no-pager | tail -20   # watch for certificate success
```

## 8. Test before handing it out

From your laptop, an MCP `initialize` call should return `200`:

```
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST https://mcp.brewerypi.com/REPLACE_WITH_SECRET/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}'
```

A path **without** the secret should return `404`.

## 9. Add it to Claude (each demo user)

Give each user the full URL:

```
https://mcp.brewerypi.com/REPLACE_WITH_SECRET/mcp
```

On **Pro or Max**: Customize → Connectors → **+** → **Add custom connector** →
paste the URL → **Add**. Then enable it per conversation via the **+** menu in
the chat. On **Team/Enterprise**, an Owner adds it once under Organization
settings → Connectors, and members connect individually.

Try: *"Browse the brewery hierarchy,"* or *"What tags are in the Brewhouse,
and what are the latest Mash Temp readings?"*

## Updating later

```
cd /opt/brewerypi
git pull
.venv/bin/pip install -e ".[mcp]"
systemctl restart brewerypi-mcp
```

## Notes

- **Rotating the secret:** change it in the Caddyfile, `systemctl reload
  caddy`, and re-share the new URL. (Removing the old connector and re-adding
  is the user-side step, since connector URLs can't be edited in place.)
- **Read-only:** the server exposes only SELECT tools, so a leaked URL means
  read access to demo data — never modification. If you later add write
  tools, upgrade the auth to OAuth (Caddy can sit in front, or FastMCP can
  enforce it) rather than relying on a secret path.
- **Backups:** the whole database is the single file `/opt/brewerypi/app.db`;
  copy it off the box to back it up.
