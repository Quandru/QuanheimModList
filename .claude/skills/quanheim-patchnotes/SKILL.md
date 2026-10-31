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
4. Write the highlights: replace `_Highlights pending._` between `<!-- highlights:start -->` and
   `<!-- highlights:end -->`. They become the Discord embed, so lay them out for easy reading:
   - 3–6 highlights. Each one is a `### ` heading with one emoji and a short headline (2–5 words, no full stop),
     then a blank line, then its text as `- ` bullets: one bullet of one or two plain sentences naming the mod,
     or a few short bullets if it covers several mods. Every line under a heading is a bullet, so the text lines
     up in Discord. Leave a blank line between highlights. Example:

     ```
     ### 🛣️ Build roads with the hoe

     - **MyDirtyHoe** adds a Terrain Road tool: click points along a route and it lays a smooth, evenly sloped road.
     ```
   - Bold the mod name the first time it appears in each highlight.
   - Write for players: what they'll notice in game, not how the code changed.
   - Most noticeable gameplay changes first. Group small fixes, library and framework bumps into one closing
     highlight (e.g. `### 🔧 Fixes and framework updates`), one bullet per mod.
   - Mention every added or removed mod.
   - Framework mods (BepInEx, Jotunn) don't have their notes quoted in the patch file. Use the `notes` that
     `prepare` printed for them, and summarise in one short line what players would care about.
   - Only state what the notes say. No invented numbers or features. If a mod published no notes, say so
     rather than guessing.
   - Keep it under 2,000 characters.
   Don't edit the quoted notes below the highlights.
5. `git add -A && git commit -m "Patch notes <date>: <n> mod changes" && git push origin main`.
   Push before posting, so the link in Discord already works.
6. `python3 tools/patchnotes.py post`. It reads `VALHEIM_PATCH_DISCORD_WEBHOOK_URL` (and `VALHEIM_PATCH_DISCORD_GROUP_TAG`, if set, to ping that role)
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
