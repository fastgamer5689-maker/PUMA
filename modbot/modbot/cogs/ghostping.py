"""Ghost-ping detection + channel-specific @everyone/@here rules.

Interpretation of the spec:
  * User pings are always recorded in the message log. If the author deletes/edits the message so a
    mentioned user is no longer pinged, it is logged, warned in-channel and counted as an infraction.
  * @everyone/@here are allowed in official channels (names containing "announcement"/"rules") unless
    used more than EVERYONE_SPAM_LIMIT times per minute. Elsewhere they are flagged (non-staff: message
    deleted; staff: logged only).
"""
from __future__ import annotations

import json
import time
from datetime import timedelta
from collections import defaultdict, deque
from contextlib import suppress

import discord
from discord.ext import commands

import config
from utils.helpers import is_staff_member, is_trusted_id


class GhostPing(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._everyone_uses: dict[tuple[int, int], deque] = defaultdict(deque)

    # ----------------------------------------------------------- ghost pings
    async def _report(self, row: dict, how: str, victims: list[int]) -> None:
        if row["ghost_reported"] or row["is_bot"] or not victims:
            return
        if time.time() - row["created_at"] > config.GHOST_PING_WINDOW:
            return
        guild = self.bot.get_guild(row["guild_id"])
        if guild is None:
            return
        await self.bot.db.execute("UPDATE messages SET ghost_reported=1 WHERE message_id=?", (row["message_id"],))
        await self.bot.db.add_infraction(guild.id, row["author_id"], "ghost_ping", f"msg {row['message_id']} ({how})")
        count = await self.bot.db.count_infractions(guild.id, row["author_id"], "ghost_ping", 86400)
        names = ", ".join(f"<@{v}>" for v in victims)
        channel = guild.get_channel_or_thread(row["channel_id"])
        if channel is not None:
            with suppress(discord.HTTPException):
                await channel.send(
                    f"⚠️ <@{row['author_id']}> ghost-pinged {names} ({how} their message). This has been logged "
                    f"(**{count}** in 24h). Please don't ping people and then remove the message.",
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=row["author_id"])]),
                    delete_after=60,
                )
        self.bot.batch.add(
            guild, "ghostping", "👻 Ghost pings",
            f"👻 <@{row['author_id']}> {how} a message pinging {names} in <#{row['channel_id']}> "
            f"(msg `{row['message_id']}`, {count}/24h). Original kept in log DB.",
            color=discord.Color.light_grey(),
        )

    @commands.Cog.listener()
    async def on_logged_delete(self, row: dict) -> None:
        ids = json.loads(row["mention_ids"] or "[]")
        await self._report(row, "deleted", ids)

    @commands.Cog.listener()
    async def on_logged_edit(self, row: dict, new_mention_ids: list[int]) -> None:
        original = json.loads(row["mention_ids"] or "[]")
        removed = [i for i in original if i not in new_mention_ids and i != row["author_id"]]
        await self._report(row, "edited", removed)

    # ------------------------------------------------------- system pings
    @staticmethod
    def _is_official(channel) -> bool:
        base = channel.parent if isinstance(channel, discord.Thread) else channel
        name = getattr(base, "name", "").lower()
        return any(k in name for k in config.OFFICIAL_CHANNEL_KEYWORDS)

    @commands.Cog.listener()
    async def on_message(self, m: discord.Message) -> None:
        if m.guild is None or m.author.bot or not isinstance(m.author, discord.Member):
            return
        if "@everyone" not in m.content and "@here" not in m.content:
            return
        if is_trusted_id(self.bot, m.guild, m.author.id):
            return
        guild, member = m.guild, m.author
        staff = is_staff_member(member)

        if self._is_official(m.channel):
            now = time.time()
            dq = self._everyone_uses[(guild.id, member.id)]
            dq.append(now)
            while dq and now - dq[0] > 60:
                dq.popleft()
            if len(dq) <= config.EVERYONE_SPAM_LIMIT:
                return  # allowed
            reason = f"@everyone/@here spam in an official channel ({len(dq)}/min)"
        else:
            reason = "@everyone/@here outside official channels"

        await self.bot.db.add_infraction(guild.id, member.id, "system_ping", f"msg {m.id}")
        action = "logged only (staff)"
        if not staff:
            with suppress(discord.HTTPException):
                await m.delete()
                action = "message deleted"
            if "spam" in reason:
                with suppress(discord.HTTPException):
                    await member.timeout(timedelta(minutes=10),
                                         reason="System-ping spam")
                    action += ", 10-minute timeout"
        self.bot.batch.add(guild, "systemping", "📣 System-ping flags",
                           f"📣 {member.mention} in {m.channel.mention}: {reason} — {action} (msg `{m.id}`).",
                           color=discord.Color.orange())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GhostPing(bot))
