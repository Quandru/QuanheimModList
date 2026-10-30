---
name: quanheim-patchnotes
description: Check the Quanheim Gale sync profile for mod changes and, if there are any, write a patch note in patchnotes/, update modlist.json, push to main and post a summary embed to Discord. Use when asked to run or check Quanheim patch notes, or when a scheduled run fires.
---

# Quanheim patch notes

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
   `<!-- highlights:end -->`. Rules:
   - 3–6 bullets, written for players: what they'll notice in game, not how the code changed.
   - Each bullet: one emoji, a **bold headline of a few words**, then one or two plain sentences naming the mod.
   - Most noticeable gameplay changes first. Group small fixes, library and dependency bumps into one closing bullet.
   - Mention every added or removed mod.
   - Only state what the quoted notes say. No invented numbers or features. If a mod published no notes, say so
     rather than guessing.
   - Keep it under 1,500 characters. It becomes the Discord embed.
   Don't edit the quoted notes below the highlights.
5. `git add -A && git commit -m "Patch notes <date>: <n> mod changes" && git push origin main`.
   Push before posting, so the link in Discord already works.
6. `python3 tools/patchnotes.py post`. It reads `DISCORD_WEBHOOK_URL` (and `DISCORD_ROLE_ID`, if set, to ping that role) from the environment, and records the message ID
   in `modlist.json`. Then commit `Record Discord post` and push.
   - If `DISCORD_WEBHOOK_URL` isn't set or the post fails, leave it. The next run sees `unposted` and retries.
     Say so in the reply.
   - Use `post --dry-run` to see the payload without sending it.

## Never
- Commit anything from the profile zip apart from what `prepare` writes. It holds server configs and player lists.
- Post to Discord before the patch note is pushed.
- Use `post --force` unless the user asks for a repost.

## Reply
One or two lines: which status, which patch file (with its GitHub link) and whether Discord was posted.
