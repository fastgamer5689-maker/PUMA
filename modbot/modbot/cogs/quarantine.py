"""Quarantine: new bots are isolated until approved; new humans get a 10-minute vetting period
limited to #joins (read-only) and #do-not-type. Messages from quarantined users are scanned and
compromised-account behaviour (invites, scam lures, links, spam) is kicked/banned immediately.

Note: members/bots holding the Administrator permission ignore channel overwrites — Discord offers no way
to hide channels from them. The bot flags such bots in the log.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from contextlib import suppress

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://|www\.", re.I)
INVITE_RE = re.compile(r"(discord\.gg|discord(app)?\.com/invite)/", re.I)
LURE_RE = re.compile(r"free\s+nitro|steam\s+gift|airdrop|claim\s+(your|now)|gift\s+card|crypto\s+giveaway|"
                     r"verify\s+your\s+account", re.I)
SPAM_MESSAGES, SPAM_SECONDS = 5, 8


class Quarantine(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active: set[tuple[int, int]] = set()          # (guild, user) currently quarantined (humans)
        self._recent: dict[tuple[int, int], deque] = defaultdict(deque)

    async def cog_load(self) -> None:
        rows = await self.bot.db.fetchall("SELECT guild_id,user_id FROM quarantine WHERE released=0 AND is_bot=0")
        self.active = {(r["guild_id"], r["user_id"]) for r in rows}
        self.release_loop.start()

    async def cog_unload(self) -> None:
        self.release_loop.cancel()

    # ------------------------------------------------------ channel overwrites
    async def apply_role_locks(self, guild: discord.Guild) -> None:
        """Hide every channel from the quarantine/isolation roles except the two allowed ones."""
        cfg = await self.bot.db.get_config(guild.id)
        q = guild.get_role(cfg.get("quarantine_role_id") or 0)
        iso = guild.get_role(cfg.get("isolation_role_id") or 0)
        joins_id, dnt_id = cfg.get("joins_channel_id"), cfg.get("dnt_channel_id")
        sem = asyncio.Semaphore(8)

        async def lock(ch: discord.abc.GuildChannel) -> None:
            async with sem:
                with suppress(discord.HTTPException):
                    if q:
                        if ch.id == joins_id:
                            want = discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                                               send_messages=False, add_reactions=False)
                        elif ch.id == dnt_id:
                            want = discord.PermissionOverwrite(view_channel=True, read_message_history=True,
                                                               send_messages=True)
                        else:
                            want = discord.PermissionOverwrite(view_channel=False)
                        if ch.overwrites_for(q) != want:
                            await ch.set_permissions(q, overwrite=want, reason="ModBot quarantine lock")
                    if iso:
                        want = discord.PermissionOverwrite(view_channel=False, send_messages=False)
                        if ch.overwrites_for(iso) != want:
                            await ch.set_permissions(iso, overwrite=want, reason="ModBot isolation lock")

        await asyncio.gather(*(lock(c) for c in guild.channels))

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        cfg = await self.bot.db.get_config(channel.guild.id)
        if cfg.get("quarantine_role_id") or cfg.get("isolation_role_id"):
            await self.apply_role_locks(channel.guild)

    # ---------------------------------------------------------------- joining
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild, db = member.guild, self.bot.db
        cfg = await db.get_config(guild.id)
        now = time.time()

        if member.bot:
            iso = guild.get_role(cfg.get("isolation_role_id") or 0)
            adder = "unknown"
            with suppress(discord.HTTPException):
                async for e in guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
                    if e.target and e.target.id == member.id:
                        adder = f"<@{e.user_id}>"
                        break
            note = ""
            if member.guild_permissions.administrator:
                note = " ⚠️ It has **Administrator**, which bypasses channel isolation — consider kicking it."
            if iso:
                with suppress(discord.HTTPException):
                    await member.add_roles(iso, reason="New bot quarantine")
            await db.execute("INSERT OR REPLACE INTO quarantine (guild_id,user_id,joined_at,release_at,is_bot,released) "
                             "VALUES (?,?,?,NULL,1,0)", (guild.id, member.id, now))
            self.bot.batch.add(guild, "quarantine", "🧪 Quarantine",
                               f"🤖 New bot {member.mention} (`{member.id}`) added by {adder} — quarantined."
                               f"{'' if iso else ' (no isolation role — run /setup)'}{note} Approve with `/bot_approve`.",
                               color=discord.Color.orange(), ping_mods=True)
            return

        role = guild.get_role(cfg.get("quarantine_role_id") or 0)
        if role is None:
            return
        release = now + config.QUARANTINE_MINUTES * 60
        with suppress(discord.HTTPException):
            await member.add_roles(role, reason="New member vetting")
        await db.execute("INSERT OR REPLACE INTO quarantine (guild_id,user_id,joined_at,release_at,is_bot,released) "
                         "VALUES (?,?,?,?,0,0)", (guild.id, member.id, now, release))
        self.active.add((guild.id, member.id))
        joins = guild.get_channel(cfg.get("joins_channel_id") or 0)
        if isinstance(joins, discord.TextChannel):
            with suppress(discord.HTTPException):
                await joins.send(f"👋 {member.mention} joined — in vetting until <t:{int(release)}:R>.",
                                 allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        self.active.discard((member.guild.id, member.id))

    # ----------------------------------------------------------------- release
    async def release_member(self, guild: discord.Guild, user_id: int) -> None:
        cfg = await self.bot.db.get_config(guild.id)
        member = guild.get_member(user_id)
        role = guild.get_role(cfg.get("quarantine_role_id") or 0)
        if member and role and role in member.roles:
            with suppress(discord.HTTPException):
                await member.remove_roles(role, reason="Vetting period finished")
        await self.bot.db.execute("UPDATE quarantine SET released=1 WHERE guild_id=? AND user_id=?", (guild.id, user_id))
        self.active.discard((guild.id, user_id))

    @tasks.loop(seconds=15)
    async def release_loop(self) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT guild_id,user_id FROM quarantine WHERE released=0 AND is_bot=0 AND release_at<=?", (time.time(),))
        for r in rows:
            guild = self.bot.get_guild(r["guild_id"])
            if guild:
                await self.release_member(guild, r["user_id"])

    @release_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # --------------------------------------------------------- threat analysis
    @commands.Cog.listener()
    async def on_message(self, m: discord.Message) -> None:
        if m.guild is None or m.author.bot or (m.guild.id, m.author.id) not in self.active:
            return
        text, key = m.content, (m.guild.id, m.author.id)
        now = time.time()
        dq = self._recent[key]
        dq.append(now)
        while dq and now - dq[0] > SPAM_SECONDS:
            dq.popleft()

        ban = bool(INVITE_RE.search(text) or LURE_RE.search(text) or "@everyone" in text or "@here" in text)
        kick = bool(URL_RE.search(text)) or len(dq) >= SPAM_MESSAGES
        if not (ban or kick):
            return
        reason = ("invite/scam lure/mass-ping" if ban else "link or spam") + " from a quarantined account"
        with suppress(discord.HTTPException):
            await m.delete()
        try:
            if ban:
                await m.guild.ban(m.author, reason=f"[Quarantine] {reason}", delete_message_seconds=3600)
                verb = "🔨 Banned"
            else:
                await m.guild.kick(m.author, reason=f"[Quarantine] {reason}")
                verb = "👢 Kicked"
        except discord.HTTPException as exc:
            verb = f"⚠️ FAILED (HTTP {exc.status}) to remove"
        self.active.discard(key)
        await self.bot.db.add_infraction(m.guild.id, m.author.id, "quarantine_threat", reason)
        self.bot.batch.add(m.guild, "quarantine", "🧪 Quarantine",
                           f"{verb} {m.author.mention} (`{m.author.id}`) — {reason}. Msg `{m.id}` kept in log DB.",
                           color=discord.Color.red(), ping_mods=True)

    # ---------------------------------------------------------------- commands
    @app_commands.command(name="bot_approve", description="Release a quarantined bot.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def handle_approve(self, interaction: discord.Interaction, bot_member: discord.Member) -> None:
        if not bot_member.bot:
            return await interaction.response.send_message("That's not a bot.", ephemeral=True)
        cfg = await self.bot.db.get_config(interaction.guild.id)
        iso = interaction.guild.get_role(cfg.get("isolation_role_id") or 0)
        if iso and iso in bot_member.roles:
            await bot_member.remove_roles(iso, reason=f"Approved by {interaction.user}")
        await self.bot.db.execute("UPDATE quarantine SET released=1 WHERE guild_id=? AND user_id=?",
                                  (interaction.guild.id, bot_member.id))
        await interaction.response.send_message(f"✅ {bot_member.mention} released from quarantine.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Quarantine(bot))
