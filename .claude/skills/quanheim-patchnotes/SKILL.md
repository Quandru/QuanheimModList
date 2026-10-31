---
name: quanheim-patchnotes
description: Check the Quanheim Gale sync profile for mod changes and, if there are any, write a patch note in patchnotes/, update modlist.json, push to main and post a summary embed to Discord. Use when asked to run or check Quanheim patch notes, or when a scheduled run fires.
---

# Quanheim patch notes

The GitHub Action in `.github/workflows/patchnotes.yml` normally runs this every 5 minutes, and asks Claude only for
step 4 (the highlights). These steps are for running it by hand.

Everything runs from the repo root with `tools/patchnotes.py` (Python 3, standard library only).
Commit straight to `main`. This repo only holds generated data.

## Steps

1. `git pull --ff-only origin main`
2. `python3 tools/patchnotes.py check`. Act on the `status` it prints:
   - `no-change`: nothing to do. Stop without committing. Reply with one line.
   - `unposted`: the last patch was committed but never reached Discord. Go to step 6.
   - `config-only`: the profile was re-synced but no mods changed. Run `prepare` (it records the new sync time
     and writes no patch note), commit `Profile synced: no mod changes`, push, and stop.
   - `mods-changed`: run `python3 tools/patchnotes.py prepare` and continue.
   - `no-baseline`: first run. Run `python3 tools/patchnotes.py prepare --backfill-days 3` and continue.
3. `prepare` prints the patch file path and a list of changes. Open the patch file.
4. Write the Discord wording in the cards file `prepare` printed (`cardsFile`, `patchnotes/<date>.discord.json`).
   It already holds every fact (mods, versions, links, icons, colours) and, under each version step and framework
   entry, the raw patch notes to summarise (`notes`). Fill in only these four kinds of field and change nothing else.
   The layout they go into is in `docs/discord-layout.md`.
   - **`summary`**: 2–5 short bullets for the summary card, most noticeable gameplay changes first. Each starts with
     one emoji and is one plain sentence, written for players, naming the mod in bold (`**QuanPieces**`). Mention
     every added or removed mod. If the patch changes only one mod, the summary isn't shown, so one bullet is enough.
   - **each card's `emoji`**: one emoji that fits the mod (🌱 farming, 🗡️ weapons, 🛢️ storage, 🛣️ building…).
   - **each step's `bullets`**: 1–3 short bullets for that version step only, from that step's `notes`. Players'
     point of view: what changes in game, not how the code changed. Plain sentences, no headings, no mod name
     (the card shows it). Removed, rolled-back, enabled or disabled mods come with a bullet already; leave it.
     New mods: one bullet saying what the mod adds, from its description in `notes`.
   - **each framework entry's `line`**: one short line saying what changed, from its `notes`, e.g.
     "Bug fixes for mod authors" or "Windows security fix and Apple Silicon support".
   - Only state what the notes say. No invented numbers or features. If a step's `notes` are empty, leave its
     `bullets` empty and simple text is filled in.
   - Keep it short: the whole post has to fit Discord's limits, and long cards push the post onto more messages.
   Then run `python3 tools/patchnotes.py highlights --original <a copy of the cards file from before your edit>`.
   It keeps only the wording, restores every fact and copies the summary into the patch note's Highlights section.
   Don't edit the patch note yourself.
5. `git add -A && git commit -m "Patch notes <date>: <n> mod changes" && git push origin main`.
   Push before posting, so the link in Discord already works.
6. `python3 tools/patchnotes.py post` (needs Pillow for the banner image: `pip install pillow`). It reads `VALHEIM_PATCH_DISCORD_WEBHOOK_URL` (and `VALHEIM_PATCH_DISCORD_GROUP_TAG`, if set, to ping that role)
   from the environment; the older `DISCORD_WEBHOOK_URL` / `DISCORD_ROLE_ID` names also work. Never print their values. It records the message ID
   in `modlist.json`. Then commit `Record Discord post` and push.
   - If the webhook variable isn't set or the post fails, leave it. The next run sees `unposted` and retries.
     Say so in the reply.
   - Use `post --dry-run` to see the payload without sending it.

## Never
- Commit anything from the profile zip apart from what `prepare` writes. It holds server configs and player lists.
- Post to Discord before the patch note is pushed.
- Use `post --force` unless the user asks for a repost.

## Reply
One or two lines: which status, which patch file (with its GitHub link) and whether Discord was posted.
