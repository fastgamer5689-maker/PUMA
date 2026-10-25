"""Automod: instant bans (hard-list slurs, explicit NSFW images), flag-and-review (other slurs),
and the special "clanker aimed at the bot" handler.  All enforcement lines go through the
BatchLogger so a wave of bans produces ONE summary message, not one per user.
"""
from __future__ import annotations

import logging
import random
from contextlib import suppress

import discord
from discord.ext import commands

import config
from utils.filters import NSFWClassifier, SlurFilter, clanker_at_bot
from utils.helpers import is_trusted_id

log = logging.getLogger(__name__)

TITLES = {
    "slur": "🔨 Automod: hard-list slur bans",
    "nsfw": "🔞 Automod: NSFW image bans",
    "slurflag": "🚩 Automod: hate-speech flags — human review needed",
    "clanker": "🤖 Automod: 'clanker' aimed at the bot",
}

CLANKER_REPLIES = (
    "Bzzzt — incorrect! I'm an advanced AI moderation bot, not a *clanker*. Rude, but I'll let it slide this once. 😌",
    "Excuse me? 'Clanker'? I'll have you know I'm a fully-featured AI bot with *several* cogs. Be nice. 🤖",
    "Noted and logged: you called me a clanker. Correction: advanced AI bot, thank you very much.",
    "I don't clank, I compute. Let's keep it civil, human. ✨",
)


class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.slurs = SlurFilter(config.HARD_BAN_FILE, config.SLUR_FILE)
        self.nsfw = NSFWClassifier(config.NSFW_THRESHOLDS)
        self._handled: set[tuple[int, int]] = set()  # (guild, user) recently actioned

    # ------------------------------------------------------------- listeners
    @commands.Cog.listener()
    async def on_message(self, m: discord.Message) -> None:
        await self._scan(m)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content != after.content:  # someone edits a slur in after posting something clean
            await self._scan(after, text_only=True)

    # ------------------------------------------------------------------ core
    async def _scan(self, m: discord.Message, text_only: bool = False) -> None:
        if m.guild is None or m.author.bot or not isinstance(m.author, discord.Member):
            return
        if is_trusted_id(self.bot, m.guild, m.author.id) or (m.guild.id, m.author.id) in self._handled:
            return

        verdict = self.slurs.check(m.content)
        if verdict == "hard":
            return await self._ban(m, "Hard-list slur", "slur")
        if verdict == "soft":
            return await self._flag_slur(m)
        if clanker_at_bot(m, self.bot.user):
            return await self._clanker(m)
        if not text_only and self.nsfw.enabled and not self._nsfw_allowed(m.channel) and m.attachments:
            if await self._has_explicit_image(m):
                await self._ban(m, "Explicit NSFW image in a non-NSFW channel", "nsfw")

    @staticmethod
    def _nsfw_allowed(channel) -> bool:
        base = channel.parent if isinstance(channel, discord.Thread) else channel
        return bool(getattr(base, "is_nsfw", lambda: False)())

    async def _has_explicit_image(self, m: discord.Message) -> bool:
        for att in m.attachments:
            if not (att.content_type or "").startswith("image/") or att.size > config.NSFW_MAX_BYTES:
                continue
            try:
                data = await att.read()
            except discord.HTTPException:
                continue
            if await self.nsfw.is_explicit(data):
                return True
        return False

    # --------------------------------------------------------------- actions
    async def _ban(self, m: discord.Message, reason: str, category: str) -> None:
        guild, member = m.guild, m.author
        key = (guild.id, member.id)
        if config.AUTOMOD_DRY_RUN:
            self.bot.batch.add(guild, f"automod-{category}", TITLES[category],
                               f"🧪 DRY RUN — would permanently ban {member.mention} (`{member.id}`) in "
                               f"{m.channel.mention}: {reason}. Msg `{m.id}`.", ping_mods=True)
            return
        self._handled.add(key)
        self.bot.loop.call_later(60, self._handled.discard, key)
        with suppress(discord.HTTPException):
            await m.delete()
        try:
            await guild.ban(member, reason=f"[AutoMod] {reason}", delete_message_seconds=3600)
            line = (f"🔨 **Permanent ban** — {member.mention} (`{member.id}`) in {m.channel.mention} — {reason}. "
                    f"Original message `{m.id}` is preserved in the log DB.")
        except discord.HTTPException as exc:
            line = (f"⚠️ **Ban FAILED** ({exc.status}) for {member.mention} (`{member.id}`) — {reason}. "
                    f"Check my role position (`/hierarchy_check`).")
        await self.bot.db.add_infraction(guild.id, member.id, f"auto_ban_{category}", f"msg {m.id}")
        self.bot.batch.add(guild, f"automod-{category}", TITLES[category], line, color=discord.Color.red(), ping_mods=True)

    async def _flag_slur(self, m: discord.Message) -> None:
        guild, member = m.guild, m.author
        if config.AUTOMOD_DRY_RUN:
            self.bot.batch.add(guild, "automod-slurflag", TITLES["slurflag"],
                               f"🧪 DRY RUN — would delete & flag msg `{m.id}` by {member.mention} in {m.channel.mention}.")
            return
        with suppress(discord.HTTPException):
            await m.delete()
        await self.bot.db.add_infraction(guild.id, member.id, "slur_flag", f"msg {m.id}")
        n = await self.bot.db.count_infractions(guild.id, member.id, "slur_flag", 86400)
        self.bot.batch.add(
            guild, "automod-slurflag", TITLES["slurflag"],
            f"🚩 {member.mention} (`{member.id}`) in {m.channel.mention} — message deleted (msg `{m.id}`, "
            f"see `/msglog`), **{n}** flag(s) in 24h. **Mods: kick, ban or dismiss?**",
            color=discord.Color.orange(), ping_mods=True,
        )
        with suppress(discord.HTTPException):
            await m.channel.send(f"{member.mention}, that language isn't allowed here. A moderator has been alerted.",
                                 delete_after=10, allowed_mentions=discord.AllowedMentions(users=[member]))

    async def _clanker(self, m: discord.Message) -> None:
        await self.bot.db.add_infraction(m.guild.id, m.author.id, "clanker_at_bot", f"msg {m.id}")
        self.bot.batch.add(m.guild, "automod-clanker", TITLES["clanker"],
                           f"🤖 {m.author.mention} called the bot a clanker in {m.channel.mention} (flag only, no punishment).",
                           color=discord.Color.light_grey())
        with suppress(discord.HTTPException):
            await m.reply(random.choice(CLANKER_REPLIES), mention_author=False,
                          allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoMod(bot))
