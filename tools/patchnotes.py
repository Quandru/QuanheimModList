#!/usr/bin/env python3
"""Quanheim patch notes: track a Gale sync profile and turn mod changes into patch notes.

Subcommands (run from the repo root):
  check                 Compare the live profile meta with modlist.json. Prints a JSON status.
  prepare [--backfill-days N]
                        Download the profile, work out the delta, fetch patch notes,
                        update modlist.json and write patchnotes/<date>.md.
  highlights --original FILE
                        After Claude has written the wording into the cards file (patchnotes/<date>.discord.json):
                        keep only the wording, restore every fact from FILE (the file as prepare wrote it), fill
                        anything left empty with simple text, and copy the summary into the patch note.
  server                Query the game server (Steam A2S, address from the Gale profile's ServerConnect config) and
                        record its Valheim version in status/server-version.json when it changes. A change leaves an
                        alert pending: the next mod post carries it as a card, or `post-server` sends it on its own.
  post-server           Post a pending server-update alert on its own (when no mod update carried it).
  post [--dry-run]      Post the latest patch to Discord, laid out as in docs/discord-layout.md
                        (env VALHEIM_PATCH_DISCORD_WEBHOOK_URL, role ping: VALHEIM_PATCH_DISCORD_GROUP_TAG).
                        The avatar is the webhook's own, set in Discord.

Standard library only, except that the Discord banner image needs Pillow (tools/images.py). Without Pillow the
banner becomes a text heading and everything else still works.
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
# Modding frameworks and libraries: their changelogs are long and technical. The patch note links to the changelog
# instead of quoting it, and Discord groups their updates into one purple card instead of a card each.
FRAMEWORKS = {"denikson-BepInExPack_Valheim", "ValheimModding-Jotunn", "ValheimModding-JsonDotNET",
              "ValheimModding-YamlDotNet", "ValMedia-OOD_LIB", "ZenDragon-Zen_ModLib"}

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
        info = mod_api(src, c["name"], c["to"] or c["from"]) or {}
        c["icon"] = info.get("icon")
        if c["change"] in ("updated", "added", "source-changed"):
            c["released"] = info.get("date_created")
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
        cards_rel = rel[:-3] + ".discord.json"
        with open(os.path.join(ROOT, cards_rel), "w") as f:
            json.dump(cards_skeleton(changes, meta, len(snap), rel), f, indent=2, ensure_ascii=False)
            f.write("\n")
        new_state["lastPatch"]["cardsFile"] = cards_rel
        result.update(status="patch-written", patchFile=rel, cardsFile=cards_rel)
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


# ---------------------------------------------------------------- discord: cards file
# Layout rules: docs/discord-layout.md. prepare writes patchnotes/<date>.discord.json with the facts; Claude fills in
# the wording (summary, emoji, bullets, framework lines); `highlights` checks it; `post` builds the messages from it.
YELLOW, GREEN, RED, PURPLE, BLUE = 0xFDCB58, 0x78B159, 0xDD2E44, 0xAA8ED6, 0x55ACEE
CARD_COLOR = {"updated": YELLOW, "source-changed": YELLOW, "added": GREEN, "enabled": GREEN,
              "removed": RED, "downgraded": RED, "disabled": RED}
LEGEND = "🟨 Updated · 🟩 New mod · 🟥 Removed / rolled back · 🟪 Framework / library"
WORDING = ("summary", "emoji", "bullets", "line")      # the only fields Claude may change

def _steps(c):
    """Version steps for one change, oldest first, each with the raw notes Claude summarises."""
    ch, frm, to = c["change"], c["from"], c["to"]
    if ch == "updated":
        notes = list(reversed(c.get("notes") or []))           # notes_between gives newest first
        steps, prev = [], frm
        for ver, text in notes:
            steps.append({"label": f"{prev} → {ver}" if prev else ver, "notes": text[:3000], "bullets": []})
            prev = ver
        if not steps or vkey(prev) != vkey(to):
            steps.append({"label": f"{prev or '?'} → {to}", "notes": "", "bullets": []})
        return steps
    fixed = {
        "added": (f"new · {to}", c.get("description") or "", []),
        "removed": (f"removed · was {frm}", "", ["Removed from the pack."]),
        "downgraded": (f"rolled back · {frm} → {to}", "", ["Rolled back to an earlier version."]),
        "enabled": (f"enabled · {to}", "", ["Turned back on in the pack."]),
        "disabled": (f"disabled · {to}", "", ["Turned off in the pack, but still installed."]),
        "source-changed": (f"{to} · {SOURCE_LABEL.get(c.get('fromSource'), '?')} → "
                           f"{SOURCE_LABEL.get(c.get('source'), '?')}", "",
                           ["Same version, now installed from a different site. No gameplay change."]),
    }[ch]
    return [{"label": fixed[0], "notes": fixed[1][:3000], "bullets": list(fixed[2])}]

def cards_skeleton(changes, meta, total, patch_file):
    ordered = sorted(changes, key=lambda c: (ORDER.index(c["change"]), pretty(c["name"]).lower()))
    cards, framework = [], []
    for c in ordered:
        ns, _ = split_name(c["name"])
        if c["name"] in FRAMEWORKS and c["change"] in ("updated", "downgraded"):
            notes = "\n\n".join(f"[{v}]\n{t}" for v, t in c.get("notes") or [])
            framework.append({"name": c["name"], "title": pretty(c["name"]), "url": c.get("url"),
                              "label": f'{c["from"] or "?"} → {c["to"]}', "notes": notes[:3000], "line": ""})
            continue
        cards.append({"name": c["name"], "title": pretty(c["name"]), "change": c["change"], "author": ns,
                      "source": SOURCE_LABEL.get(c.get("source"), c.get("source") or ""), "url": c.get("url"),
                      "icon": c.get("icon"), "emoji": "", "steps": _steps(c)})
    kinds = [c["change"] for c in changes]
    counts = {"new": kinds.count("added") + kinds.count("enabled"),
              "removed": kinds.count("removed") + kinds.count("disabled")}
    counts["updated"] = len(kinds) - counts["new"] - counts["removed"]
    return {"patchFile": patch_file, "profileUpdatedAt": meta["updatedAt"], "totalMods": total, "counts": counts,
            "summary": [], "cards": cards, "framework": framework}

def _first_lines(text, n=2):
    """Simple fallback bullets: the first n items of some notes. An item starts at a bullet or after a blank line;
    wrapped continuation lines are joined back on. Headings, version markers and tables are skipped."""
    items, cur = [], None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if (not line or line.startswith(("#", "<", "|", "_")) or set(line) <= set("-=*`")
                or re.fullmatch(r"\[?v?\d+(?:\.\d+)+\]?:?", line)):
            cur = None
            continue
        m = re.match(r"^(?:[-*+•]|\d+[.)])\s+(.*)", line)
        if m or cur is None:
            cur = [m.group(1) if m else line]
            items.append(cur)
        else:
            cur.append(line)
    out = []
    for item in items[:n]:
        l = " ".join(item)
        out.append(l if len(l) <= 160 else l[:157].rsplit(" ", 1)[0] + "…")
    return out

def finalize_cards(edited, original):
    """Keep only Claude's wording from `edited`; take every fact from `original`; fill gaps with simple text."""
    out, used_ai, used_auto = json.loads(json.dumps(original)), False, False
    def strings(v, limit):
        return [x.strip()[:300] for x in v if isinstance(x, str) and x.strip()][:limit] if isinstance(v, list) else []
    summary = strings(edited.get("summary"), 6)
    for i, card in enumerate(out["cards"]):
        e = (edited.get("cards") or [{}] * len(out["cards"]))[i] if i < len(edited.get("cards") or []) else {}
        if isinstance(e, dict) and e.get("name") == card["name"]:
            if isinstance(e.get("emoji"), str) and 0 < len(e["emoji"].strip()) <= 8:
                card["emoji"] = e["emoji"].strip()
            for j, step in enumerate(card["steps"]):
                es = (e.get("steps") or [])[j] if j < len(e.get("steps") or []) else {}
                b = strings(es.get("bullets") if isinstance(es, dict) else None, 6)
                if b:
                    step["bullets"], used_ai = b, True
        for step in card["steps"]:
            if not step["bullets"]:
                step["bullets"] = _first_lines(step["notes"]) or [
                    "The author didn't publish patch notes for this version." if card["change"] == "updated"
                    else "New to the pack."]
                used_auto = True
    for i, fw in enumerate(out["framework"]):
        e = (edited.get("framework") or [])[i] if i < len(edited.get("framework") or []) else {}
        line = e.get("line").strip()[:200] if isinstance(e, dict) and isinstance(e.get("line"), str) else ""
        if line and e.get("name") == fw["name"]:
            fw["line"], used_ai = line, True
        else:
            fw["line"] = (_first_lines(fw["notes"], 1) or ["Framework update."])[0]
            used_auto = True
    if summary:
        out["summary"], used_ai = summary, True
    else:
        out["summary"] = [f'{c["emoji"] or "📦"} **{c["title"]}** {VERB[c["change"]].lower()}'
                          for c in out["cards"]][:5] or ["Framework and library updates."]
        if out["framework"] and out["cards"]:
            out["summary"].append("🔧 Framework and library updates.")
        used_auto = True
    for step in (s for c in out["cards"] for s in c["steps"]):
        step.pop("notes", None)
    for fw in out["framework"]:
        fw.pop("notes", None)
    return out, ("mixed" if used_ai and used_auto else "ai" if used_ai else "auto")

def cmd_highlights(args):
    state = load_state()
    lp = state["lastPatch"]
    cards_path = os.path.join(ROOT, lp["cardsFile"])
    with open(args.original) as f:
        original = json.load(f)
    try:
        with open(cards_path) as f:
            edited = json.load(f)
    except (OSError, ValueError):
        edited = {}
    cards, source = finalize_cards(edited, original)
    with open(cards_path, "w") as f:
        json.dump(cards, f, indent=2, ensure_ascii=False)
        f.write("\n")
    # the patch note's Highlights section shows the same summary
    path = os.path.join(ROOT, lp["file"])
    with open(path) as f:
        md = f.read()
    head, rest = md.split(HL_START, 1)
    block = "\n".join("- " + b for b in cards["summary"])
    with open(path, "w") as f:
        f.write(head + HL_START + "\n" + block + "\n" + HL_END + rest.split(HL_END, 1)[1])
    print(json.dumps({"highlights": source, "cards": len(cards["cards"]), "framework": len(cards["framework"])}))


# ---------------------------------------------------------------- discord: messages
MAX_COMPONENTS, MAX_TEXT = 38, 3800        # Discord allows 40 and 4,000 per message; keep a margin
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

def _text(s):
    return {"type": 10, "content": s}

def _image(name):
    return {"type": 12, "items": [{"media": {"url": f"attachment://{name}"}}]}

def _container(body, accent=None, icon=None):
    inner = ({"type": 9, "components": [_text(body)], "accessory": {"type": 11, "media": {"url": icon}}}
             if icon else _text(body))
    c = {"type": 17, "components": [inner]}
    if accent is not None:
        c["accent_color"] = accent
    return c

def _size(component):
    """(components, characters of text) a block uses, counting nested components."""
    n, chars = 1, len(component.get("content", ""))
    for child in component.get("components", []) + ([component["accessory"]] if "accessory" in component else []):
        cn, cc = _size(child)
        n, chars = n + cn, chars + cc
    return n + len(component.get("items", [])), chars

def card_text(card):
    title = f'{card["emoji"]} {card["title"]}'.strip()
    lines = [f'-# by {card["author"]} · {card["source"]}',
             f'### [{title}]({card["url"]})' if card.get("url") else f"### {title}"]
    for i, step in enumerate(card["steps"]):
        if i:
            lines.append("")
        lines.append(f'`{step["label"]}`')
        lines += [f"- {b}" for b in step["bullets"]]
    text = "\n".join(lines)
    return text if len(text) <= 3000 else text[:2990].rsplit("\n", 1)[0] + "\n- …"

GENERATED = {}          # attachment name -> generated file for this post (banner.png, server-banner.png)

def build_messages(state, cards, banner_path=None):
    """List of (components, [(attachment name, source file)], ping) per message, following docs/discord-layout.md."""
    GENERATED.clear()
    GENERATED["banner.png"] = banner_path
    lp = state["lastPatch"]
    url = f"{REPO_URL}/blob/{BRANCH}/{lp['file']}"
    role = re.sub(r"^<@&|>$", "", env(*ROLE_ENV))
    ping = f"<@&{role}> new mod updates are live." if role.isdigit() else "New mod updates are live."
    synced = parse_time(cards["profileUpdatedAt"])
    head = [_text(ping + " Update via Gale before you join.")]
    k = cards["counts"]
    if banner_path:
        head.append(_image("banner.png"))
    else:
        head.append(_text(f'# Quanheim mod update\n### ⬆️ {k["updated"]} updated · ✨ {k["new"]} new · '
                          f'🗑️ {k["removed"]} removed\n-# {synced.strftime("%-d %B %Y")} · {cards["totalMods"]} mods'))
    link = f"**[📜 Read the full patch notes →]({url})**"
    if len(cards["cards"]) + len(cards["framework"]) == 1:     # one mod: its card says it all, so just the link
        head.append(_container(link))
    else:
        head.append(_container("\n".join("- " + b for b in cards["summary"]) + "\n\n" + link))
    alert = pending_server_alert()
    if alert:                                    # the server's game version changed: the red image under the header
        img = render_server_banner(alert, name_line=False) if banner_path else None
        if img:
            GENERATED["server-banner.png"] = img
            head.insert(2, _image("server-banner.png"))
        else:                                    # no image library: a plain card in the same place
            head.insert(2, server_card(alert))
    head.append(_image("rune-rule.png"))
    body = [_container(card_text(c), CARD_COLOR.get(c["change"], YELLOW), c.get("icon")) for c in cards["cards"]]
    tail = []
    if cards["framework"]:
        tail.append(_container("### Framework and library updates\n" + "\n".join(
            (f'- **[{f["title"]}]({f["url"]})**' if f.get("url") else f'- **{f["title"]}**') +
            f' `{f["label"]}`: {f["line"]}' for f in cards["framework"]), PURPLE))
    tail += [_image("rune-rule.png"),
             _container(f'-# {cards["totalMods"]} mods · Gale profile {PROFILE_CODE} · synced '
                        f'{synced.strftime("%-d %b, %H:%M")} UTC\n\n-# {LEGEND}')]      # blank line: legend sits apart

    spacer = _image("spacer.png")
    base_n, base_c = _size(spacer)
    messages, cur = [], list(head)
    def fits(blocks):
        n = 2 * base_n + sum(_size(b)[0] for b in blocks)
        return n <= MAX_COMPONENTS and sum(_size(b)[1] for b in blocks) <= MAX_TEXT
    for block in body + [tail]:                     # the tail stays together, after the last card
        group = block if isinstance(block, list) else [block]
        if not fits(cur + group) and cur:
            messages.append(cur)
            cur = []
        cur += group
    messages.append(cur)

    out = []
    for i, blocks in enumerate(messages):
        # first message: ping line, then the spacer, then the banner. Others: spacer first. All end with one.
        comps = blocks[:1] + [spacer] + blocks[1:] + [spacer] if i == 0 else [spacer] + blocks + [spacer]
        out.append(_unique_images(comps) + (i == 0 and role.isdigit(),))
    return out

def _unique_images(comps):
    """Give every image use its own attachment name. Discord shows a file attached once in only one place, so a
    second use of the same file (e.g. both rune rules) would render as an empty box."""
    comps, files, seen = json.loads(json.dumps(comps)), [], {}
    for c in comps:
        if c["type"] != 12:
            continue
        for it in c["items"]:
            name = it["media"]["url"][len("attachment://"):]
            stem = name[:-4]
            seen[stem] = seen.get(stem, 0) + 1
            unique = f"{stem}-{seen[stem]}.png"
            it["media"]["url"] = f"attachment://{unique}"
            files.append((unique, GENERATED.get(name) or os.path.join(ASSETS, name)))
    return comps, files

def _post_message(hook, comps, files, ping_role):
    payload = {"username": WEBHOOK_NAME, "flags": 1 << 15, "components": comps,
               "allowed_mentions": {"parse": [], "roles": [ping_role]} if ping_role else {"parse": []}}
    payload["attachments"] = [{"id": i, "filename": name} for i, (name, _) in enumerate(files)]
    boundary = "quanheim" + os.urandom(8).hex()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            f"Content-Type: application/json\r\n\r\n").encode() + json.dumps(payload).encode() + b"\r\n"
    for i, (name, src) in enumerate(files):
        with open(src, "rb") as fh:
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[{i}]\"; "
                     f"filename=\"{name}\"\r\nContent-Type: image/png\r\n\r\n").encode() + fh.read() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    sep = "&" if "?" in hook else "?"
    return json.loads(http(hook + sep + "wait=true&with_components=true", data=body, method="POST",
                           headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}))["id"]

def _delete_message(hook, message_id):
    try:
        http(f'{hook.split("?")[0]}/messages/{message_id}', method="DELETE")
    except urllib.error.URLError:
        pass

def render_banner(cards):
    """Banner PNG in .patchnotes/, or None (text heading instead) if Pillow or the fonts aren't available."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import images
        os.makedirs(WORK_DIR, exist_ok=True)
        k = cards["counts"]
        return images.banner(os.path.join(WORK_DIR, "banner.png"),
                             parse_time(cards["profileUpdatedAt"]).strftime("%-d %B %Y"), cards["totalMods"],
                             k["updated"], k["new"], k["removed"])
    except Exception as e:      # missing Pillow, font download failed, ...
        print(f"warning: no banner image ({e}); using a text heading", file=sys.stderr)
        return None

def cmd_post(args):
    state = load_state()
    if not state or not state.get("lastPatch"):
        sys.exit("nothing to post: no patch recorded in modlist.json")
    lp = state["lastPatch"]
    if lp["discord"].get("posted") and not args.force:
        sys.exit(f"already posted ({lp['discord'].get('messageId')}); use --force to post again")
    if not lp.get("cardsFile"):
        sys.exit("this patch has no cards file (it predates the card layout)")
    with open(os.path.join(ROOT, lp["cardsFile"])) as f:
        cards = json.load(f)
    if not cards.get("summary") and not args.force:
        sys.exit("the cards file has no summary yet; run `highlights` first")
    messages = build_messages(state, cards, None if args.dry_run else render_banner(cards))
    if args.dry_run:
        print(json.dumps([{"components": c, "files": [n for n, _ in fs], "ping": p}
                          for c, fs, p in messages], indent=1, ensure_ascii=False))
        return
    hook = env(*WEBHOOK_ENV)
    if not hook:
        sys.exit("no webhook: set %s" % " or ".join(WEBHOOK_ENV))
    role = re.sub(r"^<@&|>$", "", env(*ROLE_ENV))
    ids = []
    try:
        for comps, files, ping in messages:
            ids.append(_post_message(hook, comps, files, role if ping else None))
    except urllib.error.HTTPError as e:
        for mid in ids:                 # all or nothing, so the retry doesn't leave half a post behind
            _delete_message(hook, mid)
        sys.exit(f"Discord refused message {len(ids) + 1} of {len(messages)}: HTTP {e.code} "
                 f"{e.read()[:500].decode('utf-8', 'replace')}")
    lp["discord"] = {"posted": True, "messageId": ids[0], "messageIds": ids,
                     "postedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    save_state(state)
    if pending_server_alert():                   # the post carried the server-update card
        mark_server_alert_posted(ids[0])
    print(json.dumps({"status": "posted", "messages": len(ids), "messageIds": ids}))


# ---------------------------------------------------------------- server version
SERVER_STATE = os.path.join(ROOT, "status", "server-version.json")

def load_server_state():
    if os.path.exists(SERVER_STATE):
        with open(SERVER_STATE) as f:
            return json.load(f)
    return None

def save_server_state(st):
    os.makedirs(os.path.dirname(SERVER_STATE), exist_ok=True)
    with open(SERVER_STATE, "w") as f:
        json.dump(st, f, indent=2)
        f.write("\n")

def pending_server_alert():
    st = load_server_state()
    alert = (st or {}).get("alert")
    return alert if alert and not alert.get("posted") else None

def mark_server_alert_posted(message_id):
    st = load_server_state()
    st["alert"].update(posted=True, messageId=message_id,
                       postedAt=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    save_server_state(st)

def render_server_banner(alert, name_line):
    """The red Server Update image in .patchnotes/, or None if Pillow or the fonts aren't available."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import images
        os.makedirs(WORK_DIR, exist_ok=True)
        return images.server_banner(os.path.join(WORK_DIR, "server-banner.png"), alert["to"], alert.get("from"),
                                    name_line=name_line)
    except Exception as e:
        print(f"warning: no server banner image ({e}); using a plain card", file=sys.stderr)
        return None

def server_card(alert):
    """Fallback when the red image can't be drawn."""
    was = f' (was {alert["from"]})' if alert.get("from") else ""
    return _container(f'### 🛡️ Valheim game update\nThe server has been updated to **Valheim {alert["to"]}**{was}.\n'
                      f'- Update Valheim in Steam before you join.', BLUE)

def cmd_server(args):
    """Query the server and record its game version when it changes. Never fails the run: an unreachable server
    (e.g. mid-reboot) just records nothing, and the next run catches the new version."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import server_query
    os.makedirs(WORK_DIR, exist_ok=True)
    result = {"status": "unreachable"}
    try:
        host, port = server_query.address_from_profile()
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::add-mask::{host}")         # keep the IP out of the public log
        info = server_query.query(host, port)
        kw = dict(kv.split("=", 1) for kv in info.get("keywords", "").split(",") if "=" in kv)
        ver = {"game": kw.get("g"), "network": kw.get("n")}
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        st = load_server_state()
        if not ver["game"]:
            result = {"status": "no-version", "keywords": info.get("keywords")}
        elif st is None:
            save_server_state({"current": {**ver, "since": now}, "history": [{**ver, "since": now}], "alert": None})
            result = {"status": "baseline", **ver}
        elif (st["current"]["game"], st["current"].get("network")) == (ver["game"], ver["network"]):
            result = {"status": "same", **ver}
        else:
            prev = st["current"]
            st["current"] = {**ver, "since": now}
            st["history"].append({**ver, "since": now})
            st["alert"] = {"from": prev["game"], "to": ver["game"], "detectedAt": now, "posted": False}
            save_server_state(st)
            result = {"status": "changed", "from": prev["game"], **ver}
    except Exception as e:                       # unreachable, profile download failed, ...
        result["error"] = f"{type(e).__name__}: {e}"[:200]
    with open(os.path.join(WORK_DIR, "server.json"), "w") as f:
        json.dump(result, f)
    print(json.dumps(result))

def cmd_post_server(args):
    alert = pending_server_alert()
    if not alert:
        print(json.dumps({"status": "nothing-pending"}))
        return
    role = re.sub(r"^<@&|>$", "", env(*ROLE_ENV))
    ping = f"<@&{role}> the Valheim server has been updated." if role.isdigit() else "The Valheim server has been updated."
    spacer = _image("spacer.png")
    GENERATED.clear()
    img = None if args.dry_run else render_server_banner(alert, name_line=True)
    if img:
        GENERATED["server-banner.png"] = img
    comps, files = _unique_images([_text(ping), spacer, _image("server-banner.png") if img else server_card(alert),
                                   spacer])
    if args.dry_run:
        print(json.dumps(comps, indent=1, ensure_ascii=False))
        return
    hook = env(*WEBHOOK_ENV)
    if not hook:
        sys.exit("no webhook: set %s" % " or ".join(WEBHOOK_ENV))
    try:
        mid = _post_message(hook, comps, files, role if role.isdigit() else None)
    except urllib.error.HTTPError as e:
        sys.exit(f"Discord refused the server alert: HTTP {e.code} {e.read()[:500].decode('utf-8', 'replace')}")
    mark_server_alert_posted(mid)
    print(json.dumps({"status": "posted", "messageId": mid}))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check"); p.set_defaults(fn=cmd_check)
    p.add_argument("--record", action="store_true", help="record this check; keepalive when due")
    p = sub.add_parser("highlights"); p.set_defaults(fn=cmd_highlights)
    p.add_argument("--original", required=True, help="copy of the cards file as prepare wrote it")
    p = sub.add_parser("prepare"); p.set_defaults(fn=cmd_prepare)
    p.add_argument("--backfill-days", type=float, default=0,
                   help="first run only: treat versions released in the last N days as this patch")
    sub.add_parser("server").set_defaults(fn=cmd_server)
    p = sub.add_parser("post-server"); p.set_defaults(fn=cmd_post_server)
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("post"); p.set_defaults(fn=cmd_post)
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--force", action="store_true")
    args = ap.parse_args()
    args.fn(args)

if __name__ == "__main__":
    main()
