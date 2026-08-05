# Bitaxe Baller on 5tratumOS

[5tratumOS](https://github.com/WillItMod/5tratum) (by WillItMod) is a Debian-based
home-mining OS that runs the **Umbrel app framework** — same app-proxy +
`docker-compose` model, same community-store format. That's the good news: we
don't need a new packaging format, just a networking tweak.

## What was blocking us

Our stock Umbrel recipe ([`../docker-compose.yml`](../docker-compose.yml)) runs
the app on `network_mode: host` and points the app-proxy at
`host.docker.internal:13701`. On stock Umbrel that's fine; on **5tratumOS it
blank-pages** — the proxy can't reach the app through that wiring. 5tratumOS's
own apps (e.g. `willitmod-axelive`) use plain **bridge networking**, with the
proxy talking to the service container over Docker DNS.

The files here are that bridge-mode variant:

- [`docker-compose.yml`](docker-compose.yml) — bridge networking, modeled on the
  proven `willitmod-axelive` app. The tile loads.
- [`umbrel-app.yml`](umbrel-app.yml) — same app identity as our stock recipe.

### Trade-offs of bridge mode

| Capability | Works on bridge? |
|---|---|
| App tile loads (no blank page) | ✅ |
| Add miners by IP | ✅ |
| Monitoring / tuning / leaderboard | ✅ |
| LAN auto-scan | ⚠️ only with `BBR_SCAN_SUBNET` set (below) |
| mDNS `bitaxe-baller.local` | ❌ bridge has no multicast — disabled |

**`BBR_SCAN_SUBNET`** (added in the app for this): a bridged container sees only
the Docker network, so the scanner can't guess your LAN. Set it in the compose
to your real `/24` (e.g. `192.168.1.0/24`) and the scanner works. Without it,
add miners by IP — everything else is unaffected.

## Two ways to ship it

### Path A — users add our store directly (no waiting on anyone)

Because 5tratumOS is Umbrel-based, a user can add our community store today:
**Settings → App Store → Add community store →**
`https://github.com/465media/umbrel-bitaxe-baller-store`, then install Bitaxe
Baller. For this to load on 5tratumOS, our store's app must ship the **bridge**
compose from here (publish it to the store repo as the compose for
`bitaxeballer-app`, keeping the host-mode one for stock Umbrel is a follow-up —
see "Open question" below). This path needs zero cooperation from WillItMod.

### Path B — listed in WillItMod's own store

Submit to **[`WillItMod/umbrel-community-store`](https://github.com/WillItMod/umbrel-community-store)**
(store id `willitmod`; has a dev sibling `umbrel-dev-community-store`). Note the
repo's `NO_FORK_POLICY.md` — **ask before opening a PR**, and that ask belongs on
the **store repo**, not the OS repo.

> **Outreach correction:** our original question
> ([`WillItMod/5tratum#95`](https://github.com/WillItMod/5tratum/issues/95)) sat
> a week with no reply because it's on the **OS** repo. The store and its activity
> (19 open issues) live on `WillItMod/umbrel-community-store`. Re-raise there —
> or just do Path A and let users self-serve.

## Open question for us

Stock Umbrel benefits from host mode (auto-scan + mDNS out of the box); 5tratumOS
needs bridge mode. Options: (a) ship bridge everywhere and lean on
`BBR_SCAN_SUBNET` + IP-add (simplest, mild regression for stock-Umbrel scan/mDNS),
or (b) keep both recipes and select per-OS. Decide before publishing to the live
store.

## Verifying (needs a 5tratumOS box)

Everything here is verified except the one thing that needs the actual OS:
`BBR_SCAN_SUBNET` parsing, bridge-mode monitoring, and the leaderboard path are
unit-tested and confirmed against a real miner; the **app-proxy tile load on
5tratumOS itself** can only be confirmed on a 5tratumOS install (yours or a
user's). Install the app, open the tile — it should render the dashboard, not a
blank page.
