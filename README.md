# Quanheim mod list

The mod pack for the Quanheim Valheim server, tracked from the Gale sync profile **`ZKG7UK`**,
with patch notes for every change.

- **[`modlist.json`](modlist.json):** every mod in the pack with its version, whether it's enabled, and where it's
  installed from (Thunderstore or Hexium). Mods changed in the most recent patch carry a `lastPatch` entry
  (`change`, `from`, `to`). Unchanged mods don't.
- **[`patchnotes/`](patchnotes/):** one file per patch, named by the profile's sync time (`YYYY-MM-DD-HHMM.md`).
  Each starts with highlights and an at-a-glance table, followed by each mod's own patch notes quoted from its
  changelog. Modding frameworks (BepInEx, Jotunn) are summarised in the highlights and linked, not quoted:
  their changelogs are long and technical.

## How it works

1. **Check:** read the profile's meta (`/api/profile/ZKG7UK/meta`, a few KB) and compare it with `modlist.json`.
   If nothing changed, stop.
2. **Detail:** if mods changed, download the profile and read only `export.r2x` for each mod's source.
   The server's config files in that download are never stored here.
3. **Delta:** find what was updated, added, removed, enabled, disabled or rolled back. For updates, collect the
   notes for *every* version between the old one and the new one, from Thunderstore or Hexium.
4. **Write:** update `modlist.json` and write the patch note. Claude writes the highlights.
5. **Publish:** push to `main`, then post an embed to Discord with the highlights and a link to the full notes.

## Automation

A GitHub Action ([`.github/workflows/patchnotes.yml`](.github/workflows/patchnotes.yml)) runs every 5 minutes.
Most runs only check the profile and stop. When mods change, it prepares the patch note, runs Claude Code
(signed in with a Claude subscription token) to write the highlights, pushes, and posts to Discord. If Claude
can't run, simple highlights are filled in so the patch still goes out. Once a day, or whenever the status
changes, it writes [`status/last-check.json`](status/last-check.json). That also stops GitHub pausing the schedule
after 60 days without commits.

Repository secrets (Settings → Secrets and variables → Actions):

| Secret | What it is |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | From `claude setup-token`, run on your own machine. Uses your Claude subscription. |
| `VALHEIM_PATCH_DISCORD_WEBHOOK_URL` | The Discord webhook to post to. |
| `VALHEIM_PATCH_DISCORD_GROUP_TAG` | Optional: the ID of the role to ping. |

The steps live in [`.claude/skills/quanheim-patchnotes/SKILL.md`](.claude/skills/quanheim-patchnotes/SKILL.md).
The mechanics live in [`tools/patchnotes.py`](tools/patchnotes.py).

## Running it by hand

```bash
python3 tools/patchnotes.py check            # status only
python3 tools/patchnotes.py prepare          # write modlist.json + patch note
python3 tools/patchnotes.py post --dry-run   # show the Discord payload
VALHEIM_PATCH_DISCORD_WEBHOOK_URL=... python3 tools/patchnotes.py post
```

Settings (environment variables): `VALHEIM_PATCH_DISCORD_WEBHOOK_URL` (required for posting),
`VALHEIM_PATCH_DISCORD_GROUP_TAG` (optional role ID to ping; the older names `DISCORD_WEBHOOK_URL` and
`DISCORD_ROLE_ID` still work), plus optional `GALE_PROFILE_CODE`, `PATCHNOTES_REPO_URL`, `PATCHNOTES_BRANCH` and `PATCHNOTES_WEBHOOK_NAME`.

Patch notes are quoted from each mod author's changelog or README, with links to the originals.
