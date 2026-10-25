# ModBot

Modular discord.py 2.x moderation bot (Python 3.10+).

## Run
1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill it in.
3. Developer Portal -> Bot: enable **Server Members Intent** and **Message Content Intent**.
4. Invite with permissions: Manage Channels, Manage Roles, Ban/Kick Members, Moderate Members,
   Manage Messages, View Audit Log, Manage Webhooks, Send Messages, Embed Links, Attach Files.
5. `python main.py`
6. In each server: drag the bot's role to the very top, run `/hierarchy_check`, then `/setup mod_role:@Mods`.
7. Fill `filters/hard_ban_terms.txt` and `filters/slur_terms.txt` (one term per line). Keep `AUTOMOD_DRY_RUN=true`
   until the mod-log shows no false positives, then set it to `false`.
8. `/ticket_panel` in a public help channel; `/faq_add` for the questions the ticket bot should answer.

## Layout
- `database.py` - SQLite schema (messages, edits, channel snapshots, blacklist, quarantine, tickets...)
- `utils/batch_logger.py` - one consolidated mod-log message per action sequence
- `utils/filters.py` - slur/profanity/tragedy/clanker matching + NudeNet NSFW scanner
- `cogs/` - onboarding, logging_cog, automod, antinuke, ghostping, quarantine, tickets, ai_chat

## Slash commands
`/setup` `/hierarchy_check` `/msglog` `/unfreeze` `/snapshot` `/restore_channels` `/incidents`
`/trial_resolve` `/authorize_staff` `/bot_approve` `/ticket_panel` `/faq_add` `/faq_remove`

## Known limits
- A bot cannot move its own role; it can only remind you.
- It cannot ban/strip anyone whose role is above its own, and Administrator bypasses channel overwrites.
- Anti-nuke attribution needs the View Audit Log permission.
- The freeze only changes @everyone's overwrite; channels where another role explicitly allows sending stay open to that role.
- NSFW scanning covers image attachments only (first frame of GIFs); videos are not scanned.
- Rollback restores channels, categories and their overwrites; it does not restore roles, messages or webhooks
  (messages remain readable in the log DB).
- Message logging stores member content permanently: disclose it in your server rules / privacy notice.
