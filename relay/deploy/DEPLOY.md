# Deploying the relay to bitaxe-baller-site VPS

End-to-end runbook for putting `relay.bitaxeballer.com` live. Assumes the
existing VPS already runs the bitaxeballer.com marketing site behind nginx
with Let's Encrypt — we ride alongside it.

Stateless, single-instance, in-memory. Restart is cheap. If the box ever
needs to move, the only persistent thing is `/etc/bitaxe-baller-relay.env`.

---

## One-time setup (do once, ever)

### 1. DNS

Add an A record (or CNAME pointing at the same target as `bitaxeballer.com`):

    relay.bitaxeballer.com.   IN  A   <VPS-IPv4>

Wait until it resolves — `dig +short relay.bitaxeballer.com` should return the VPS IP.

### 2. System user

The relay should not run as root. Create a dedicated unprivileged user:

    sudo useradd --system --home /opt/bitaxe-baller-relay --shell /usr/sbin/nologin bitaxe-relay

### 3. Code

    sudo mkdir -p /opt/bitaxe-baller-relay
    sudo chown bitaxe-relay:bitaxe-relay /opt/bitaxe-baller-relay
    sudo -u bitaxe-relay -H bash -c '
      cd /opt/bitaxe-baller-relay &&
      git clone https://github.com/465media/bitaxe-baller.git tmp &&
      cp -r tmp/relay/* . &&
      rm -rf tmp &&
      python3 -m venv venv &&
      venv/bin/pip install -r requirements.txt
    '

For updates later: `cd /opt/bitaxe-baller-relay && sudo -u bitaxe-relay git pull` won't work because we only copied `relay/*`. Easiest update path: re-run the above with a fresh clone. Stateless service — restart is the source of truth.

### 4. Env file

    sudo cp /opt/bitaxe-baller-relay/deploy/env.example /etc/bitaxe-baller-relay.env
    sudo chown root:root /etc/bitaxe-baller-relay.env
    sudo chmod 0600 /etc/bitaxe-baller-relay.env

Then edit `/etc/bitaxe-baller-relay.env` and replace the `RELAY_SECRET=…` placeholder with the output of:

    python3 -c "import secrets; print(secrets.token_hex(32))"

**Back the secret up in 1Password.** Rotating it logs everyone out.

### 5. Systemd unit

    sudo cp /opt/bitaxe-baller-relay/deploy/bitaxe-baller-relay.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now bitaxe-baller-relay

Verify it's listening:

    curl -s http://127.0.0.1:8787/health
    # → {"ok":true,"connected_apps":0,"version":"0.1.0"}

Logs: `journalctl -u bitaxe-baller-relay -f`

### 6. nginx + TLS

Add the server block (existing nginx, just adding a new vhost):

    sudo cp /opt/bitaxe-baller-relay/deploy/nginx.conf.example \
            /etc/nginx/sites-available/relay.bitaxeballer.com
    sudo ln -s /etc/nginx/sites-available/relay.bitaxeballer.com \
               /etc/nginx/sites-enabled/

Make sure the `map $http_upgrade $connection_upgrade` block is in your
top-level `http {}` (in `/etc/nginx/nginx.conf` or `/etc/nginx/conf.d/`).
The relay's nginx file has the snippet at the bottom — paste it once at
the http level if it isn't there.

Issue the cert (certbot rewrites the file in place):

    sudo certbot --nginx -d relay.bitaxeballer.com

Reload nginx:

    sudo nginx -t && sudo systemctl reload nginx

### 7. Smoke test from outside the VPS

From your laptop:

    curl https://relay.bitaxeballer.com/health
    # → {"ok":true, ...}

If you have a real Pro license activated locally with remote-access
enabled and pointed at the prod URL, it should show up in
`connected_apps`:

    curl https://relay.bitaxeballer.com/health
    # → {"ok":true, "connected_apps":1, ...}

Then open `https://relay.bitaxeballer.com/` in a browser, enter your
license key, and confirm the SPA loads + shows your devices.

---

## Updating to a new version

Stateless makes this easy:

    sudo systemctl stop bitaxe-baller-relay
    sudo -u bitaxe-relay -H bash -c '
      cd /opt/bitaxe-baller-relay &&
      git clone --depth 1 https://github.com/465media/bitaxe-baller.git /tmp/bb-update &&
      rm -rf main.py protocol.py registry.py licensing.py tokens.py config.py README.md tests/ web/ deploy/ requirements.txt &&
      cp -r /tmp/bb-update/relay/* . &&
      rm -rf /tmp/bb-update &&
      venv/bin/pip install -q --upgrade -r requirements.txt
    '
    sudo systemctl start bitaxe-baller-relay
    curl -s http://127.0.0.1:8787/health

Active sessions die because state is in-memory. Connected apps reconnect
automatically (relay_client backoff loop). Connected browsers see their WS
close and need to refresh — they keep their session token in
localStorage, so they don't need to log in again.

If the update changed the WS protocol in a backward-incompatible way,
older desktop clients will fail to register until they update. None of
the v1.9.0 changes break that contract.

---

## Rolling back

If something is wrong with a new release:

    sudo systemctl stop bitaxe-baller-relay
    cd /opt/bitaxe-baller-relay
    sudo -u bitaxe-relay git -C /tmp/bb-update checkout <previous-tag>
    # then re-copy as in "Updating", but from the older tag

In practice the relay is so small that re-deploying from a previous git
checkout is the simplest "rollback."

---

## Monitoring suggestions

Not strictly required but cheap to set up:

- Uptime Kuma (or any uptime probe) hitting `https://relay.bitaxeballer.com/health` every minute. Alert on 4xx/5xx or non-200.
- Watch the `connected_apps` count in /health — sudden drops to zero across all licenses suggest the relay is healthy but apps can't reach it (firewall, cert expiry, DNS).
- `journalctl -u bitaxe-baller-relay --since '1 hour ago'` for ad-hoc tails. Errors there are usually license validation failures or WS protocol violations from buggy clients — useful signal.

---

## What to do if the secret leaks

Rotate `RELAY_SECRET` in `/etc/bitaxe-baller-relay.env`, then:

    sudo systemctl restart bitaxe-baller-relay

Every session token is invalidated; every active client gets a 4401 on
its next request and has to re-login. The license keys themselves are
unaffected — they don't live in the relay.
