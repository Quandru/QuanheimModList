#!/usr/bin/env python3
"""Quanheim patch notes: track a Gale sync profile and turn mod changes into patch notes.

Subcommands (run from the repo root):
  check                 Compare the live profile meta with modlist.json. Prints a JSON status.
  prepare [--backfill-days N]
                        Download the profile, work out the delta, fetch patch notes,
                        update modlist.json and write patchnotes/<date>.md.
  highlights --original FILE
                        After an AI edit: keep only the highlights block from the edited patch note (restore
                        everything else from FILE). If no highlights were written, fill in simple ones.
  post [--dry-run]      Post the latest patch note to Discord
                        (env VALHEIM_PATCH_DISCORD_WEBHOOK_URL, role ping: VALHEIM_PATCH_DISCORD_GROUP_TAG).

Standard library only, so it runs anywhere Python 3.9+ is available.
"""
import argparse, datetime as dt, html, io, json, os, re, sys, urllib.error, urllib.request, zipfile

PROFILE_CODE = os.environ.get("GALE_PROFILE_CODE", "ZKG7UK")
REPO_URL = os.environ.get("PATCHNOTES_REPO_URL", "https://github.com/Quandru/QuanheimModList")
BRANCH = os.environ.get("PATCHNOTES_BRANCH", "main")
WEBHOOK_NAME = os.environ.get("PATCHNOTES_WEBHOOK_NAME", "Quanheim Mod Updates")

GALE = "https://gale.kesomannen.com/api/profile"
API = {"thunderstore": "https://thunderstore.io/api/experimental/package",
       "hexium": "https://hexium.gg/api/experimental/package"}
PAGE = {"thunderstore": "https://thunderstore.io/c/valheim/p/{ns}/{name}/",
        "hexium": "https://valheim.hexium.gg/mods/{ns}/{name}"}
SOURCE_LABEL = {"thunderstore": "Thunderstore", "hexium": "Hexium"}
CHANGELOG_PAGE = {"thunderstore": "https://thunderstore.io/c/valheim/p/{ns}/{name}/changelog/",
                  "hexium": "https://valheim.hexium.gg/mods/{ns}/{name}"}
# Modding frameworks: their changelogs are long and technical. They appear in the table and the Discord
# summary, but the patch note links to the changelog instead of quoting it.
FRAMEWORKS = {"denikson-BepInExPack_Valheim", "ValheimModding-Jotunn"}

ROOT = os.getcwd()
STATE = os.path.join(ROOT, "modlist.json")
LAST_CHECK = os.path.join(ROOT, "status", "last-check.json")
WORK_DIR = os.path.join(ROOT, ".patchnotes")      # scratch, git-ignored
# Keepalive: GitHub pauses scheduled workflows after 60 days without commits. If nothing else has been committed
# for this long, a run commits status/last-check.json. Every other commit refreshes it too (see the workflow).
HEARTBEAT_MINUTES = float(os.environ.get("PATCHNOTES_HEARTBEAT_MINUTES", str(7 * 24 * 60)))
NOTES_DIR = os.path.join(ROOT, "patchnotes")
HL_START, HL_END = "<!-- highlights:start -->", "<!-- highlights:end -->"
HL_PLACEHOLDER = "_Highlights pending._"
UA = {"User-Agent": "QuanheimModList-patchnotes (+%s)" % REPO_URL}

def env(*names):
    """First non-empty environment variable among `names`."""
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""

# Environment names: VALHEIM_PATCH_* (current) with the older DISCORD_* names as a fallback.
WEBHOOK_ENV = ("VALHEIM_PATCH_DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_URL")
ROLE_ENV = ("VALHEIM_PATCH_DISCORD_GROUP_TAG", "DISCORD_ROLE_ID")


# ---------------------------------------------------------------- helpers
def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})}, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()

def get_json(url):
    return json.loads(http(url))

def vstr(v):
    return "%d.%d.%d" % (v["major"], v["minor"], v["patch"]) if isinstance(v, dict) else v

def vkey(s):
    parts = [int(p) for p in re.findall(r"\d+", s)]
    return tuple(parts + [0] * (4 - len(parts)))

def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))

def split_name(full):
    ns, name = full.split("-", 1)
    return ns, name

def pretty(name):
    return split_name(name)[1].replace("_", " ")

def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return None

def save_state(state):
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------- profile
def fetch_meta():
    return get_json(f"{GALE}/{PROFILE_CODE}/meta")

def fetch_sources():
    """Read per-mod source (Thunderstore/Hexium) from export.r2x inside the profile zip.

    The zip also holds the server's config files; we read only export.r2x and never store the rest.
    """
    z = zipfile.ZipFile(io.BytesIO(http(f"{GALE}/{PROFILE_CODE}")))
    r2x = z.read("export.r2x").decode("utf-8")
    sources, current = {}, None
    for line in r2x.splitlines():
        m = re.match(r"^- name:\s*(\S+)", line)
        if m:
            current = m.group(1)
        m = re.match(r"^\s+source:\s*(\w+)", line)
        if m and current:
            sources[current] = m.group(1).lower()
    return sources

def snapshot(meta):
    return {m["name"]: {"version": vstr(m["version"]), "enabled": m["enabled"]}
            for m in meta["manifest"]["mods"]}


# ---------------------------------------------------------------- changelog parsing
# "### 1.2.3", "## Version 1.2.3", "### v1.2.7/1.2.8 (date)", "# New in version 0.20.0"
MD_MARK = re.compile(r"^\s*(#{1,6})\s*[\*_`\[]*(?:(?:new in )?version\s+)?v?(\d+(?:\.\d+)+)(?:/v?\d+(?:\.\d+)+)*\b", re.I)
# bare lines: "1.2.3", "v1.2.3 - date", "`v19.0.0`", "**1.2.3**"
BARE_MARK = re.compile(r"^\s*[\*_`\[]*(?:version\s+)?v?(\d+(?:\.\d+)+)(?:/v?\d+(?:\.\d+)+)*[\*_`\]]*\s*([-–(:].*)?$", re.I)
PIPE_ROW = re.compile(r"^\s*\|\s*[\*_`]*v?(\d+(?:\.\d+)+)[\*_`]*\s*\|(.*)\|\s*$")
HTML_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)

def _html_to_md(s):
    items = re.findall(r"<li>(.*?)</li>", s, re.S)
    lines = ["- " + i for i in items] if items else [re.sub(r"<br\s*/?>", "\n", s)]
    return "\n".join(html.unescape(re.sub(r"<[^>]+>", "", l)).strip() for l in lines).strip()

def version_sections(md):
    """Return {version: notes} for every version found in a changelog or README."""
    if not md:
        return {}
    out = {}
    if "<tr" in md:                                   # HTML table: <td>ver</td><td>notes</td>
        for row in HTML_ROW.findall(md):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if len(cells) >= 2:
                ver = re.sub(r"<[^>]+>|[\s`v]", "", cells[0])
                if re.fullmatch(r"\d+(?:\.\d+)+", ver):
                    out[ver] = _html_to_md(cells[1])
        if out:
            return out
    lines = [re.sub(r"^\s*>\s?", "", l) for l in md.replace("\r", "").splitlines()]
    pipe = [(PIPE_ROW.match(l)) for l in lines]
    if sum(1 for p in pipe if p) >= 2:                # markdown pipe table
        for p in pipe:
            if p:
                cell = p.group(2).split("|")[0] if p.group(2).count("|") else p.group(2)
                cell = re.sub(r"<br\s*/?>", "\n", cell).strip()
                out[p.group(1)] = "\n".join(l.strip() for l in cell.splitlines() if l.strip())
        return out
    # headings (### 1.2.3) take priority; otherwise bare version lines (1.2.3 / v1.2.3 - date)
    marks = [(i, len(m.group(1)), m.group(2)) for i, l in enumerate(lines) if (m := MD_MARK.match(l))]
    if marks:
        level = min(lvl for _, lvl, _ in marks)
        # use the most common heading level that carries versions, falling back to the highest
        levels = [lvl for _, lvl, _ in marks]
        level = max(set(levels), key=levels.count) if levels else level
        marks = [(i, v) for i, lvl, v in marks if lvl == level]
        stop = re.compile(r"^\s*#{1,%d}\s" % level)
    else:
        marks = [(i, m.group(1)) for i, l in enumerate(lines) if (m := BARE_MARK.match(l))]
        stop = None
    for n, (i, ver) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        body = []
        for l in lines[i + 1:end]:
            if stop and stop.match(l):
                break
            body.append(l)
        text = "\n".join(body).strip()
        if text and ver not in out:
            out[ver] = text
    return out

VER_TOKEN = re.compile(r"(?<![\d.])v?(\d+(?:\.\d+)+)(?![\d.])", re.I)

def loose_section(md, ver):
    """Fallback for one-off layouts: find the line that names `ver` near its start (or in a heading),
    keep the rest of that line plus following lines, stop at the next line that names another version."""
    lines = [re.sub(r"^\s*>\s?", "", l) for l in (md or "").replace("\r", "").splitlines()]
    def lead_version(l):
        core = re.sub(r"^[\s#*\-|`_\[]+", "", l)
        m = VER_TOKEN.search(core[:40])
        return (m, core) if m else (None, core)
    for i, l in enumerate(lines):
        m, core = lead_version(l)
        if not (m and vkey(m.group(1)) == vkey(ver)) and not (l.lstrip().startswith("#") and ver in l):
            continue
        head = re.match(r"^\s*(#{1,6})\s", l)
        rest = core[m.end():] if m else ""
        rest = re.sub(r"^[\s|)\]:\-–*`_]*(\d{1,4}[./-]\d{1,2}[./-]\d{1,4})?[\s|)\]:\-–*`_]*", "", rest)
        rest = rest.strip().rstrip("|").strip()
        body = [("**%s**" % rest) if head and rest else rest] if rest else []
        for nxt in lines[i + 1:]:
            if head and re.match(r"^\s*#{1,%d}\s" % len(head.group(1)), nxt):
                break
            m2, _ = lead_version(nxt)
            if m2 and vkey(m2.group(1)) != vkey(ver):
                break
            if re.match(r"^\s*</?details|^\s*<summary", nxt):
                continue
            body.append(nxt)
        text = "\n".join(body).strip()
        if text:
            return text
    return None

def demote_headings(text, floor=5):
    """Shift markdown headings so the biggest one becomes level `floor`."""
    levels = [len(m.group(1)) for m in re.finditer(r"^(#{1,6})\s", text, re.M)]
    if not levels:
        return text
    shift = floor - min(levels)
    return re.sub(r"^(#{1,6})(\s)", lambda m: "#" * min(6, len(m.group(1)) + shift) + m.group(2), text, flags=re.M)

def mod_api(source, name, ver=None, kind=""):
    ns, n = split_name(name)
    url = f"{API[source]}/{ns}/{n}/" + (f"{ver}/" if ver else "") + (f"{kind}/" if kind else "")
    try:
        return get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise

def notes_between(source, name, old, new):
    """Patch notes for every version in (old, new]. Returns (list of (ver, text), where_found)."""
    for kind in ("changelog", "readme"):
        doc = mod_api(source, name, new, kind) or {}
        secs = version_sections(doc.get("markdown") or "")
        if any(vkey(v) == vkey(new) for v in secs):
            lo = vkey(old) if old else None
            picked = [(v, t) for v, t in secs.items()
                      if vkey(v) <= vkey(new) and (lo is None or vkey(v) > lo)]
            picked.sort(key=lambda vt: vkey(vt[0]), reverse=True)
            return picked, kind
    for kind in ("changelog", "readme"):        # one-off layouts: current version only
        text = loose_section((mod_api(source, name, new, kind) or {}).get("markdown"), new)
        if text:
            return [(new, text)], kind
    return [], None

def previous_version(source, name, ver):
    """Best guess at the version before `ver`, from the changelog's version list."""
    for kind in ("changelog", "readme"):
        doc = mod_api(source, name, ver, kind) or {}
        older = sorted((v for v in version_sections(doc.get("markdown") or "") if vkey(v) < vkey(ver)), key=vkey)
        if older:
            return older[-1]
    return None


# ---------------------------------------------------------------- delta
def compute_changes(old_mods, new_snap, sources):
    """old_mods: {name: {version, enabled, source}} from modlist.json."""
    changes = []
    for name, cur in new_snap.items():
        src = sources.get(name)
        prev = old_mods.get(name)
        if prev is None:
            changes.append({"name": name, "change": "added", "from": None, "to": cur["version"], "source": src})
            continue
        a, b = vkey(prev["version"]), vkey(cur["version"])
        kind = None
        if b > a: kind = "updated"
        elif b < a: kind = "downgraded"
        elif prev.get("source") and src and prev["source"] != src: kind = "source-changed"
        if kind:
            changes.append({"name": name, "change": kind, "from": prev["version"], "to": cur["version"],
                            "source": src, "fromSource": prev.get("source")})
        if prev["enabled"] != cur["enabled"]:
            changes.append({"name": name, "change": "enabled" if cur["enabled"] else "disabled",
                            "from": prev["version"], "to": cur["version"], "source": src})
    for name, prev in old_mods.items():
        if name not in new_snap:
            changes.append({"name": name, "change": "removed", "from": prev["version"], "to": None,
                            "source": prev.get("source")})
    return changes

def enrich(changes):
    """Attach release info and patch notes to each change."""
    for c in changes:
        src = c.get("source")
        if src not in API:
            continue
        ns, n = split_name(c["name"])
        c["url"] = PAGE[src].format(ns=ns, name=n)
        if c["change"] in ("updated", "added", "source-changed"):
            info = mod_api(src, c["name"], c["to"]) or {}
            c["released"] = info.get("date_created")
            c["icon"] = info.get("icon")
            c["description"] = info.get("description")
        if c["change"] == "updated":
            c["notes"], c["notesFrom"] = notes_between(src, c["name"], c["from"], c["to"])
    return changes


# ---------------------------------------------------------------- markdown
ORDER = ["updated", "added", "removed", "downgraded", "enabled", "disabled", "source-changed"]
VERB = {"updated": "Updated", "added": "Added", "removed": "Removed", "downgraded": "Rolled back",
        "enabled": "Enabled", "disabled": "Disabled", "source-changed": "Moved source"}

def fmt_date(s):
    d = parse_time(s)
    return d.strftime("%-d %b %Y"), d.strftime("%-d %b %Y, %H:%M UTC")

def change_label(c):
    if c["change"] in ("updated", "downgraded"):
        return f'`{c["from"] or "?"}` → `{c["to"]}`'
    if c["change"] == "added":
        return f'new · `{c["to"]}`'
    if c["change"] == "removed":
        return f'removed · was `{c["from"]}`'
    if c["change"] == "source-changed":
        return f'`{c["to"]}` · {SOURCE_LABEL.get(c.get("fromSource"), "?")} → {SOURCE_LABEL.get(c["source"], "?")}'
    return f'{c["change"]} · `{c["to"]}`'

def render_markdown(changes, profile_updated, prev_updated, total_mods):
    day, stamp = fmt_date(profile_updated)
    counts = {k: sum(1 for c in changes if c["change"] == k) for k in ORDER}
    chips = " · ".join(f"**{n}** {VERB[k].lower()}" for k, n in counts.items() if n)
    out = [f"# Quanheim mod update · {day}", "",
           f"> {chips}  ",
           f"> Profile synced {stamp} · {total_mods} mods in the pack" +
           (f" · previous sync {fmt_date(prev_updated)[1]}" if prev_updated else ""), "",
           "## Highlights", "", HL_START, HL_PLACEHOLDER, HL_END, "",
           "## At a glance", "", "| Mod | Change | Author | Source |", "|---|---|---|---|"]
    ordered = sorted(changes, key=lambda c: (ORDER.index(c["change"]), pretty(c["name"]).lower()))
    for c in ordered:
        ns, _ = split_name(c["name"])
        link = f'[{pretty(c["name"])}]({c["url"]})' if c.get("url") else pretty(c["name"])
        out.append(f'| {link} | {change_label(c)} | {ns} | {SOURCE_LABEL.get(c.get("source"), "—")} |')
    out += ["", "## Patch notes", ""]
    for c in ordered:
        ns, _ = split_name(c["name"])
        out.append(f'### {pretty(c["name"])} · {change_label(c)}')
        meta = [f"by **{ns}**"]
        if c.get("url"):
            meta.append(f'[{SOURCE_LABEL.get(c["source"], "page")}]({c["url"]})')
        is_fw = c["name"] in FRAMEWORKS
        if c.get("released"):
            meta.append("released " + fmt_date(c["released"])[0])
        out += ["", "<sub>" + " · ".join(meta) + "</sub>", ""]
        if c["change"] == "updated" and is_fw:
            ns_, n_ = split_name(c["name"])
            log = CHANGELOG_PAGE.get(c.get("source"), "{ns}").format(ns=ns_, name=n_)
            out.append(f"_Modding framework update. Its notes are long and technical, so they aren't copied here: "
                       f"[read the changelog]({log})._")
        elif c["change"] == "updated":
            notes = c.get("notes") or []
            if not notes:
                out.append("_The author didn't publish patch notes for this version._")
            for ver, text in notes:
                if len(notes) > 1:
                    out += [f"#### {ver}", ""]
                out += [demote_headings(text), ""]
        elif c["change"] == "added":
            out.append(f'_New to the pack._ {c.get("description") or ""}'.strip())
        elif c["change"] == "removed":
            out.append("_Removed from the pack._")
        elif c["change"] == "downgraded":
            out.append("_Rolled back to an earlier version._")
        elif c["change"] == "source-changed":
            out.append("_Same version, now installed from a different site. No gameplay change._")
        else:
            out.append(f'_{VERB[c["change"]]} in the pack._')
        out += ["", "---", ""]
    out.append(f"<sub>Generated from Gale sync profile `{PROFILE_CODE}`. Patch notes are quoted from each mod's "
               "changelog or README; follow the links for the originals.</sub>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- commands
def record_check(status, meta):
    """Write this check to .patchnotes/last-check.json (scratch). Also write it to status/last-check.json
    when the keepalive is due, i.e. nothing has refreshed that file for HEARTBEAT_MINUTES."""
    now = dt.datetime.now(dt.timezone.utc)
    body = json.dumps({"checkedAt": now.isoformat(timespec="seconds"), "status": status,
                       "profileUpdatedAt": meta["updatedAt"], "mods": len(meta["manifest"]["mods"]),
                       "runUrl": os.environ.get("RUN_URL")}, indent=2) + "\n"
    os.makedirs(WORK_DIR, exist_ok=True)
    with open(os.path.join(WORK_DIR, "last-check.json"), "w") as f:
        f.write(body)
    prev = {}
    if os.path.exists(LAST_CHECK):
        with open(LAST_CHECK) as f:
            prev = json.load(f)
    if prev and (now - parse_time(prev["checkedAt"])).total_seconds() < HEARTBEAT_MINUTES * 60:
        return False
    os.makedirs(os.path.dirname(LAST_CHECK), exist_ok=True)
    with open(LAST_CHECK, "w") as f:
        f.write(body)
    return True

def cmd_check(args):
    state = load_state()
    meta = fetch_meta()
    snap = snapshot(meta)
    if state is None:
        status = "no-baseline"
    elif state.get("lastPatch") and not state["lastPatch"].get("discord", {}).get("posted"):
        status = "unposted"
    elif state["profile"]["updatedAt"] == meta["updatedAt"]:
        status = "no-change"
    else:
        old = {m["name"]: {"version": m["version"], "enabled": m["enabled"]} for m in state["mods"]}
        status = "mods-changed" if old != snap else "config-only"
    out = {"status": status, "profileUpdatedAt": meta["updatedAt"],
           "recordedUpdatedAt": state["profile"]["updatedAt"] if state else None}
    if args.record:
        out["lastCheckWritten"] = record_check(status, meta)
    print(json.dumps(out))

def cmd_prepare(args):
    state = load_state()
    meta = fetch_meta()
    snap = snapshot(meta)
    sources = fetch_sources()
    missing = [n for n in snap if n not in sources]
    if missing:
        print(f"warning: no source in export.r2x for {missing}", file=sys.stderr)

    if state is None:
        old_mods = {}
        changes = []
        if args.backfill_days:            # first run: treat recent releases as this patch
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.backfill_days)
            for name, cur in snap.items():
                src = sources.get(name)
                if src not in API:
                    continue
                info = mod_api(src, name, cur["version"]) or {}
                if info.get("date_created") and parse_time(info["date_created"]) >= cutoff:
                    changes.append({"name": name, "change": "updated", "source": src, "to": cur["version"],
                                    "from": previous_version(src, name, cur["version"])})
        prev_updated = None
    else:
        old_mods = {m["name"]: m for m in state["mods"]}
        changes = compute_changes(old_mods, snap, sources)
        prev_updated = state["profile"]["updatedAt"]

    enrich(changes)
    changed = {c["name"]: c for c in changes if c["change"] != "removed"}
    mods = []
    for name in sorted(snap, key=str.lower):
        src = sources.get(name)
        ns, n = split_name(name)
        entry = {"name": name, "version": snap[name]["version"], "enabled": snap[name]["enabled"],
                 "source": src, "url": PAGE[src].format(ns=ns, name=n) if src in PAGE else None}
        if name in changed:   # only mods that changed in the latest patch carry this
            c = changed[name]
            entry["lastPatch"] = {"change": c["change"], "from": c["from"], "to": c["to"]}
        mods.append(entry)

    new_state = {
        "profile": {"code": PROFILE_CODE, "name": meta["manifest"]["profileName"],
                    "community": meta["manifest"]["community"], "updatedAt": meta["updatedAt"]},
        "lastPatch": state.get("lastPatch") if state else None,
        "mods": mods,
    }
    result = {"status": "no-mod-changes", "patchFile": None, "changes": len(changes)}
    if changes:
        stamp = parse_time(meta["updatedAt"]).strftime("%Y-%m-%d-%H%M")
        rel = f"patchnotes/{stamp}.md"
        os.makedirs(NOTES_DIR, exist_ok=True)
        with open(os.path.join(ROOT, rel), "w") as f:
            f.write(render_markdown(changes, meta["updatedAt"], prev_updated, len(snap)))
        new_state["lastPatch"] = {
            "file": rel, "profileUpdatedAt": meta["updatedAt"], "previousProfileUpdatedAt": prev_updated,
            "counts": {k: n for k in ORDER if (n := sum(1 for c in changes if c["change"] == k))},
            "removed": [{"name": c["name"], "version": c["from"]} for c in changes if c["change"] == "removed"],
            "discord": {"posted": False, "messageId": None},
        }
        result.update(status="patch-written", patchFile=rel)
        result["mods"] = []
        for c in changes:
            m = {"name": c["name"], "change": c["change"], "from": c["from"], "to": c["to"],
                 "notesFrom": c.get("notesFrom"), "notesVersions": [v for v, _ in c.get("notes") or []]}
            if c["name"] in FRAMEWORKS and c.get("notes"):   # not in the patch file; for the highlights only
                m["framework"] = True
                m["notes"] = "\n\n".join(f"[{v}]\n{t}" for v, t in c["notes"])[:4000]
            result["mods"].append(m)
    save_state(new_state)
    print(json.dumps(result, indent=1))


def auto_highlights(state):
    """Plain highlights built from the changes, used when no AI summary was written."""
    lp = state["lastPatch"]
    with open(os.path.join(ROOT, lp["file"])) as f:
        md = f.read()
    lines = ["### 📦 What changed", ""]
    for m in sorted((m for m in state["mods"] if m.get("lastPatch")), key=lambda m: pretty(m["name"]).lower()):
        p = m["lastPatch"]
        line = f'- **{pretty(m["name"])}** {VERB[p["change"]].lower()}'
        if p["change"] in ("updated", "downgraded"):
            line += f' to {p["to"]}'
            if m["name"] not in FRAMEWORKS:     # first line of its quoted notes, if any
                sec = md.split(f'### {pretty(m["name"])} · ', 1)
                if len(sec) > 1:
                    body = sec[1].split("\n---\n", 1)[0].split("</sub>", 1)[-1]
                    first = next((l.strip() for l in body.splitlines()
                                  if l.strip() and not l.lstrip().startswith(("#", "_", "<", "|"))), "")
                    first = re.sub(r"^[-*+]\s+|^\d+\.\s+", "", first)
                    if first:
                        line += ": " + (first if len(first) <= 160 else first[:157].rsplit(" ", 1)[0] + "…")
        lines.append(line)
    for r in lp.get("removed", []):
        lines.append(f'- **{pretty(r["name"])}** removed')
    lines += ["", "-# Summary generated automatically."]
    return "\n".join(lines)

def cmd_highlights(args):
    state = load_state()
    path = os.path.join(ROOT, state["lastPatch"]["file"])
    with open(path) as f:
        edited = f.read()
    with open(args.original) as f:
        original = f.read()
    try:
        block = edited.split(HL_START, 1)[1].split(HL_END, 1)[0].strip()
    except IndexError:
        block = ""
    source = "ai"
    if not block or HL_PLACEHOLDER in block:
        block, source = auto_highlights(state), "auto"
    head, rest = original.split(HL_START, 1)
    with open(path, "w") as f:
        f.write(head + HL_START + "\n" + block + "\n" + HL_END + rest.split(HL_END, 1)[1])
    print(json.dumps({"highlights": source, "chars": len(block)}))

def _chunks(lines, limit=1024):
    buf = ""
    for l in lines:
        if len(buf) + len(l) + 1 > limit:
            yield buf
            buf = ""
        buf += l + "\n"
    if buf:
        yield buf

FIELD_ICON = {"updated": "⬆️", "added": "✨", "removed": "🗑️", "downgraded": "⏪", "enabled": "✅",
              "disabled": "⏸️", "source-changed": "🔀"}
FIELD_BULLET = {"added": "✨", "enabled": "✅", "disabled": "⏸️"}

def build_payload(state):
    lp = state["lastPatch"]
    with open(os.path.join(ROOT, lp["file"])) as f:
        md = f.read()
    hl = md.split(HL_START, 1)[1].split(HL_END, 1)[0].strip()
    title = md.splitlines()[0].lstrip("# ").strip()
    url = f"{REPO_URL}/blob/{BRANCH}/{lp['file']}"
    if len(hl) > 3800:
        hl = hl[:3800].rsplit("\n", 1)[0] + "\n…"
    # Discord trims trailing blank lines, so a zero-width space keeps the gap before the rule.
    desc = hl + "\n\u200b\n" + "━" * 22
    by_kind = {}
    for m in state["mods"]:
        if m.get("lastPatch"):
            by_kind.setdefault(m["lastPatch"]["change"], []).append(m)
    fields = []
    for kind in ORDER:
        if kind == "removed":
            items = [f'❌ **{pretty(r["name"])}** · was `{r["version"]}`' for r in lp.get("removed", [])]
        else:
            items = []
            for m in sorted(by_kind.get(kind, []), key=lambda m: pretty(m["name"]).lower()):
                p = m["lastPatch"]
                ver = f'`{p["from"] or "?"} → {p["to"]}`' if kind in ("updated", "downgraded") else f'`{p["to"]}`'
                name = f'[{pretty(m["name"])}]({m["url"]})' if m.get("url") else pretty(m["name"])
                items.append(f'{FIELD_BULLET.get(kind, "▸")} **{name}** · {ver}')
        for n, chunk in enumerate(_chunks(items)):
            label = f'{FIELD_ICON.get(kind, "")} {VERB[kind]} ({len(items)})'.strip()
            fields.append({"name": label + (" (cont.)" if n else ""), "value": chunk.strip(), "inline": False})
    fields = fields[:24]
    fields.append({"name": "\u200b", "value": f"**[📜 Read the full patch notes →]({url})**", "inline": False})
    embed = {"title": "🛠️ " + title, "url": url, "description": desc, "color": 0xC8A04B,
             "fields": fields,
             "footer": {"text": f"{len(state['mods'])} mods · Gale profile {PROFILE_CODE}"},
             "timestamp": lp["profileUpdatedAt"]}
    payload = {"username": WEBHOOK_NAME, "embeds": [embed], "allowed_mentions": {"parse": []}}
    role = re.sub(r"^<@&|>$", "", env(*ROLE_ENV))   # accept "123..." or "<@&123...>"
    if role.isdigit():   # mentions only ping from message content, not from inside an embed
        payload["content"] = f"<@&{role}> new mod updates are live. Update via Gale before you join."
        payload["allowed_mentions"] = {"parse": [], "roles": [role]}
    return payload

def cmd_post(args):
    state = load_state()
    if not state or not state.get("lastPatch"):
        sys.exit("nothing to post: no patch recorded in modlist.json")
    lp = state["lastPatch"]
    if lp["discord"].get("posted") and not args.force:
        sys.exit(f"already posted ({lp['discord'].get('messageId')}); use --force to post again")
    payload = build_payload(state)
    if HL_PLACEHOLDER in payload["embeds"][0]["description"] and not args.force:
        sys.exit("highlights are still the placeholder; write them in the patch note first")
    if args.dry_run:
        print(json.dumps(payload, indent=1, ensure_ascii=False))
        return
    hook = env(*WEBHOOK_ENV)
    if not hook:
        sys.exit("no webhook: set %s" % " or ".join(WEBHOOK_ENV))
    sep = "&" if "?" in hook else "?"
    resp = json.loads(http(hook + sep + "wait=true", data=json.dumps(payload).encode(),
                           headers={"Content-Type": "application/json"}, method="POST"))
    lp["discord"] = {"posted": True, "messageId": resp.get("id"),
                     "postedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    save_state(state)
    print(json.dumps({"status": "posted", "messageId": resp.get("id")}))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check"); p.set_defaults(fn=cmd_check)
    p.add_argument("--record", action="store_true", help="record this check; keepalive when due")
    p = sub.add_parser("highlights"); p.set_defaults(fn=cmd_highlights)
    p.add_argument("--original", required=True, help="copy of the patch note from before the AI edit")
    p = sub.add_parser("prepare"); p.set_defaults(fn=cmd_prepare)
    p.add_argument("--backfill-days", type=float, default=0,
                   help="first run only: treat versions released in the last N days as this patch")
    p = sub.add_parser("post"); p.set_defaults(fn=cmd_post)
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--force", action="store_true")
    args = ap.parse_args()
    args.fn(args)

if __name__ == "__main__":
    main()
