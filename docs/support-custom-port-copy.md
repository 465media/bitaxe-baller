# Support-page copy: "Running on a custom port"

Drop-in section for `support.html` on bitaxeballer.com (matches the existing
support-page tone). Covers what the current page is missing: how to pin a
*specific* port via the `PORT` environment variable, including the desktop
(double-click app) cases where you can't just set it in a shell.

---

## Running on a custom port

By default the app tries to bind **port 80** (so you get a clean URL like
`http://bitaxe-baller.local`) and falls back to **`:5050`** if it can't —
which is normal when it isn't running as an administrator. If you'd rather
pin an exact port (for example, port 80 is taken by another service, or you
want a fixed `:8080`), you have two options.

### Easiest: the in-app setting (v1.23.0+)

On the machine running Bitaxe Baller, click the **port NNNN** link in the
dashboard footer, type the port you want, and **Save** — or click **Auto** to
go back to the automatic 80-then-5050 choice. The change takes effect the next
time you start the app. (The link is hidden on Umbrel/Docker, where the port is
managed for you.)

### Advanced: the `PORT` environment variable

Setting the **`PORT`** environment variable also works and takes precedence over
both the in-app setting and the automatic choice. Use this for headless/CLI runs
or scripted setups.

### Command line / self-hosted

```bash
PORT=8080 python app.py
```

### macOS app

The Mac app is launched by double-clicking, so it doesn't see the `PORT` you'd
set in a terminal. Use either of these:

**One-off — launch the app binary directly with the port set:**

```bash
PORT=8080 "/Applications/Bitaxe Baller.app/Contents/MacOS/Bitaxe Baller"
```

**Persistent — register the variable for GUI apps, then launch normally:**

```bash
launchctl setenv PORT 8080
open -a "Bitaxe Baller"
```

To undo the persistent setting: `launchctl unsetenv PORT`. (It also clears on
reboot unless you add it to a Login Item / LaunchAgent.)

### Windows app

Set `PORT` as a user environment variable, then start the app again:

```
setx PORT 8080
```

Close and reopen Bitaxe Baller so it picks up the new value. To remove it
later, set it back to empty (`setx PORT ""`) or delete it under
**Settings → System → About → Advanced system settings → Environment
Variables**.

### Umbrel

No action needed — Umbrel already runs the app on ports **13700/13701** to
avoid conflicts with other apps, and manages this for you.

> **Note:** the port you choose applies to every URL the app advertises,
> including the `bitaxe-baller.local` mDNS address, so links stay correct.
