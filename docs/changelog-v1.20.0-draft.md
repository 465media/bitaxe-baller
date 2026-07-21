# Changelog draft — v1.20.0

Feature: in-app custom listen port.

Paste-ready copy for the two places release notes live (both in separate repos,
so this is a hand-off — nothing here is auto-propagated):

- Website changelog: `bitaxe-baller-site` → `public/changelog.html` (newest first)
- Umbrel: `umbrel/umbrel-app.yml` `releaseNotes` + the mirror in
  `465media/umbrel-bitaxe-baller-store`

---

## Short form (Umbrel releaseNotes / one-liner)

> v1.20.0 — Set a custom dashboard port. A new "port" control in the dashboard
> footer lets you pin the exact port Bitaxe Baller listens on (instead of the
> automatic 80-then-5050 pick), or reset it back to automatic. Handy when port
> 80 is taken or you want a fixed address. Takes effect after you restart the
> app. Umbrel/Docker are unaffected — they keep using their configured port.

## Long form (website changelog entry)

**v1.20.0 — Custom dashboard port**

By default Bitaxe Baller tries to serve the dashboard on port 80 (for a clean
`http://bitaxe-baller.local` URL) and falls back to `:5050` when it can't. You
can now pin a specific port instead: click **port NNNN** in the dashboard footer,
enter the port you want, and Save — or hit **Auto** to go back to the automatic
choice. The change applies the next time you start the app.

This is useful when something else already owns port 80, or when you just want a
stable, predictable address. The setting only appears on the machine actually
running Bitaxe Baller, and Umbrel/Docker installs are unaffected (they continue
to use the port configured for the container).

Power users: the `PORT` environment variable still works and takes precedence
over the in-app setting.
