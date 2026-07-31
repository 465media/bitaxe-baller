#!/usr/bin/env python3
"""
Release text-surgery helper for release.sh — all the fiddly, error-prone edits
that used to be done by hand every release, in one testable place.

Two subcommands:

  bump   <version> --root <repo>
         Bumps every version string in the main repo:
           - app.py            APP_VERSION = "X.Y.Z"
           - templates/dashboard.html + device.html   footer "vX.Y.Z"
           - build/bitaxe-baller.spec   version= + CFBundleShortVersionString + CFBundleVersion
           - CLAUDE.md         leading **vX.Y.Z** header
         Idempotent: a file already at the target version is left untouched.

  notes  <version> --notes <file.md> --root <repo> [--site <site_repo>]
         Prepends the release entry to:
           - umbrel/umbrel-app.yml   releaseNotes  (prose paragraph)
           - <site>/public/changelog.html   (HTML <h2> + bullets), if --site given
         Idempotent: skips if an entry for this version is already present.

The notes file is plain markdown. Line 1 is the title (a leading '#'/'##' and a
'vX.Y.Z —' prefix are stripped). The body may use '- ' bullets, **bold**, and
`code`. GitHub release notes use the file verbatim (release.sh passes it straight
to `gh`); this helper only derives the umbrel + changelog variants from it.

Pure stdlib. No network. Exit non-zero on any failure so release.sh halts.
"""
import argparse
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _write(p: Path, s: str) -> None:
    p.write_text(s, encoding="utf-8")


def _sub_once(path: Path, pattern: str, repl: str, label: str, flags=0) -> bool:
    """Replace all matches of `pattern` in `path`. Returns True if the file
    changed. Raises if the pattern matched nothing (a silent no-op here means a
    version string quietly didn't bump — worse than a loud failure)."""
    text = _read(path)
    new, n = re.subn(pattern, repl, text, flags=flags)
    if n == 0:
        raise SystemExit(f"[release_prep] {label}: pattern not found in {path} — refusing to continue")
    if new != text:
        _write(path, new)
        return True
    return False


# ---------- bump ----------

def cmd_bump(version: str, root: Path) -> None:
    changed = []

    def note(p, did):
        changed.append(f"  {'bumped ' if did else 'ok (already) '} {p.relative_to(root)}")

    # app.py — the source of truth the update banner compares against.
    p = root / "app.py"
    note(p, _sub_once(p, r'^APP_VERSION = "[0-9]+\.[0-9]+\.[0-9]+"',
                      f'APP_VERSION = "{version}"', "app.py APP_VERSION", flags=re.M))

    # Footers on both pages.
    for tpl in ("templates/dashboard.html", "templates/device.html"):
        p = root / tpl
        note(p, _sub_once(p, r'Bitaxe Baller v[0-9]+\.[0-9]+\.[0-9]+',
                          f'Bitaxe Baller v{version}', f"{tpl} footer"))

    # PyInstaller spec — three strings (macOS bundle Info.plist).
    p = root / "build/bitaxe-baller.spec"
    t = _read(p)
    t2 = re.sub(r'version="[0-9]+\.[0-9]+\.[0-9]+"', f'version="{version}"', t)
    t2 = re.sub(r'"CFBundleShortVersionString": "[0-9]+\.[0-9]+\.[0-9]+"',
                f'"CFBundleShortVersionString": "{version}"', t2)
    t2 = re.sub(r'"CFBundleVersion": "[0-9]+\.[0-9]+\.[0-9]+"',
                f'"CFBundleVersion": "{version}"', t2)
    if 'version="' not in t2:
        raise SystemExit("[release_prep] spec: version= not found — refusing to continue")
    if t2 != t:
        _write(p, t2)
    note(p, t2 != t)

    # umbrel-app.yml version field (the releaseNotes prose is added by `notes`).
    p = root / "umbrel/umbrel-app.yml"
    note(p, _sub_once(p, r'^version: "[0-9]+\.[0-9]+\.[0-9]+"',
                      f'version: "{version}"', "umbrel-app.yml version", flags=re.M))

    # CLAUDE.md leading header **vX.Y.Z** — first occurrence only.
    p = root / "CLAUDE.md"
    note(p, _sub_once(p, r'\*\*v[0-9]+\.[0-9]+\.[0-9]+\*\*',
                      f'**v{version}**', "CLAUDE.md header", flags=0) if
         re.search(r'\*\*v[0-9]+\.[0-9]+\.[0-9]+\*\*', _read(p)) else False)

    print(f"[release_prep] bump → {version}")
    print("\n".join(changed))


# ---------- notes parsing ----------

def _parse_notes(notes_path: Path):
    raw = _read(notes_path).strip()
    if not raw:
        raise SystemExit(f"[release_prep] notes file is empty: {notes_path}")
    lines = raw.splitlines()
    title = lines[0].strip()
    title = re.sub(r'^#+\s*', '', title)                 # strip markdown heading
    title = re.sub(r'^v[0-9]+\.[0-9]+\.[0-9]+\s*[—\-:]\s*', '', title)  # strip "vX.Y.Z —"
    body = "\n".join(lines[1:]).strip()
    return title, body


_BOLD = re.compile(r'\*\*(.+?)\*\*')
_CODE = re.compile(r'`(.+?)`')


def _md_inline_to_html(s: str) -> str:
    from html import escape
    s = escape(s)
    s = _BOLD.sub(r'<strong>\1</strong>', s)
    s = _CODE.sub(r'<code>\1</code>', s)
    return s


def _md_to_bullets(body: str):
    """Return a list of bullet strings (markdown '- ' items). If the body has no
    bullets, treat each paragraph as one bullet so the changelog still renders."""
    bullets = [re.sub(r'^-\s+', '', ln).strip() for ln in body.splitlines() if ln.strip().startswith('- ')]
    if bullets:
        return bullets
    return [para.strip().replace("\n", " ") for para in re.split(r'\n\s*\n', body) if para.strip()]


def _plaintext_paragraph(title: str, body: str) -> str:
    """Flatten the notes into a single prose paragraph for umbrel releaseNotes."""
    parts = _md_to_bullets(body)
    flat = " ".join(re.sub(r'\s+', ' ', _BOLD.sub(r'\1', _CODE.sub(r'\1', p))).strip().rstrip('.') + '.'
                    for p in parts)
    return f"{title}. {flat}".strip()


def _wrap(text: str, width: int, indent: str) -> str:
    import textwrap
    return "\n".join(textwrap.wrap(text, width=width, initial_indent=indent,
                                   subsequent_indent=indent))


# ---------- notes: umbrel ----------

def _prepend_umbrel(version: str, title: str, body: str, root: Path) -> None:
    p = root / "umbrel/umbrel-app.yml"
    t = _read(p)
    if f"v{version} —" in t or f"v{version} -" in t:
        print(f"[release_prep] umbrel-app.yml already has v{version} — skipping")
        return
    para = _plaintext_paragraph(title, body)
    entry = _wrap(f"v{version} — {para}", width=88, indent="  ")
    # Insert directly after the 'releaseNotes: >-' line.
    m = re.search(r'^(releaseNotes: >-\n)', t, flags=re.M)
    if not m:
        raise SystemExit("[release_prep] umbrel-app.yml: 'releaseNotes: >-' not found")
    at = m.end()
    new = t[:at] + entry + "\n\n\n" + t[at:]
    _write(p, new)
    print(f"[release_prep] umbrel-app.yml ← v{version} releaseNotes")


# ---------- notes: site changelog ----------

def _changelog_entry_html(version: str, title: str, body: str, iso_date: str) -> str:
    lis = "\n".join(f"    <li>{_md_inline_to_html(b)}</li>" for b in _md_to_bullets(body))
    tag = f"https://github.com/465media/bitaxe-baller/releases/tag/v{version}"
    return (
        f'  <h2>v{version} — {_md_inline_to_html(title)}</h2>\n'
        f'  <p class="subtitle" style="margin-bottom: 16px;">Released {iso_date} · '
        f'<a href="{tag}" target="_blank" rel="noopener">GitHub release</a></p>\n'
        f'  <ul>\n{lis}\n  </ul>\n\n'
    )


def _prepend_changelog(version: str, title: str, body: str, site: Path, iso_date: str) -> None:
    p = site / "public/changelog.html"
    t = _read(p)
    if f'>v{version} —' in t or f'>v{version} -' in t:
        print(f"[release_prep] changelog.html already has v{version} — skipping")
        return
    # Insert immediately before the first existing "<h2>vX.Y.Z" entry.
    m = re.search(r'^\s*<h2>v[0-9]+\.[0-9]+\.[0-9]+', t, flags=re.M)
    if not m:
        raise SystemExit("[release_prep] changelog.html: no existing <h2>vX.Y.Z anchor found")
    at = m.start()
    entry = _changelog_entry_html(version, title, body, iso_date)
    new = t[:at] + entry + t[at:]
    _write(p, new)
    print(f"[release_prep] changelog.html ← v{version} entry ({iso_date})")


def cmd_notes(version: str, notes_path: Path, root: Path, site: Path | None, iso_date: str) -> None:
    title, body = _parse_notes(notes_path)
    _prepend_umbrel(version, title, body, root)
    if site:
        _prepend_changelog(version, title, body, site, iso_date)


# ---------- cli ----------

def _valid_version(v: str) -> str:
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', v):
        raise SystemExit(f"[release_prep] version must be X.Y.Z (got '{v}')")
    return v


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Release text-surgery helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bump")
    b.add_argument("version")
    b.add_argument("--root", required=True)

    t = sub.add_parser("title")
    t.add_argument("version")
    t.add_argument("--notes", required=True)

    n = sub.add_parser("notes")
    n.add_argument("version")
    n.add_argument("--notes", required=True)
    n.add_argument("--root", required=True)
    n.add_argument("--site", default=None)
    n.add_argument("--date", default=None, help="YYYY-MM-DD (default: today, local)")

    a = ap.parse_args(argv)
    version = _valid_version(a.version)

    if a.cmd == "title":
        title, _ = _parse_notes(Path(a.notes).resolve())
        print(title)
    elif a.cmd == "bump":
        cmd_bump(version, Path(a.root).resolve())
    elif a.cmd == "notes":
        iso = a.date or date.today().isoformat()
        cmd_notes(version, Path(a.notes).resolve(), Path(a.root).resolve(),
                  Path(a.site).resolve() if a.site else None, iso)


if __name__ == "__main__":
    main()
