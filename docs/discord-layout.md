# Discord post layout

How a patch is posted to Discord. Agreed 31 Oct 2026, after test posts in the channel. The code in
`tools/patchnotes.py` (`build_messages`) follows these rules; change them here first.

Posts use Discord's **Components V2** message format (sent with `with_components=true` and flag `1 << 15`),
because it allows blocks in any order and cards without a coloured edge. Plain embeds can't do either.

## Order of blocks

1. **Ping line** (first message only): `@Valheim new mod updates are live. Update via Gale before you join.`
2. **Spacer**: a 1 px high, banner-width, fully transparent image. *Every* message starts and ends with one
   (in the first message, the opening spacer comes after the ping line). It forces every message to the same width
   and gives each message the same gap above and below. Every image use is attached under its own file name:
   Discord shows a file in only one place per message, so a reused name renders as an empty box.
3. **Banner image** (first message only): generated per patch. Dark stone, bronze frame on the very edge,
   "ᛩ QUANHEIM ᛩ", "Mod update", the date and mod count, and Updated / New / Removed counts.
4. **Summary card**: no coloured edge, no image. 2–5 short bullets for players, then
   **📜 Read the full patch notes →** linking to the patch note on GitHub. When a patch changes only one mod
   (framework updates included), the summary card holds just the link: the mod's own card says it all.
5. **Rune rule**: a gold line with **ᛩᚢᚪᚾᚻᛖᛁᛗ** (QUANHEIM in Anglo-Saxon runes), on a transparent image
   with room above and below.
6. **One card per mod**, in the order updated → added → removed → rolled back → enabled → disabled → moved.
   - Coloured edge by change: 🟨 updated, 🟩 new or enabled, 🟥 removed, rolled back or disabled.
   - The mod's own icon (from Thunderstore or Hexium) in the corner.
   - `-# by Author · Source`, then the mod name as a heading linking to its page.
   - **One version block per version step, oldest first**: a code-style line such as `2.6.4 → 2.6.5` with
     its own bullets underneath, then `2.6.5 → 2.6.6` with its bullets, and so on.
7. **Framework and library card** (only when one changed): a single card with a 🟪 purple edge and no image,
   one bullet per framework or library (`FRAMEWORKS` in the code), each `**Name** old → new: one line`.
8. **Rune rule** again.
9. **Footer card**: no coloured edge, no image. The mod count, Gale profile and sync time, then the legend:
   `🟨 Updated · 🟩 New mod · 🟥 Removed / rolled back · 🟪 Framework / library`.

## Splitting

- A message holds at most 40 components and 4,000 characters of text. Messages are packed to those limits
  (with a safety margin) and split only at a card boundary.
- Only the first message pings and has the banner. Later messages carry cards only (plus their spacers),
  and the last one ends with the framework card, the rune rule and the footer.
- Every message starts and ends with a spacer, so widths and gaps match whether a patch is one message or
  several. Discord repeats the bot's name on each later message; that can't be changed.
