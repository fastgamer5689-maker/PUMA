"""Server onboarding: role-hierarchy notice on join, /setup, /hierarchy_check."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.helpers import MODLOG_NAMES, hierarchy_ok

log = logging.getLogger(__name__)

REQUIRED_PERMS = (
    "Manage Channels, Manage Roles, Ban Members, Kick Members, Moderate Members, "
    "Manage Messages, View Audit Log, Manage Webhooks"
)


class Onboarding(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------ join
    async def _find_private_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        me = guild.me

        def usable(ch: discord.TextChannel) -> bool:
            return ch.permissions_for(me).send_messages and not ch.permissions_for(guild.default_role).view_channel

        for name in MODLOG_NAMES + ("admin", "staff", "moderators"):
            for ch in guild.text_channels:
                if ch.name == name and usable(ch):
                    return ch
        for ch in guild.text_channels:
            if any(k in ch.name for k in ("mod", "admin", "staff")) and usable(ch):
                return ch
        return None

    def _notice_embed(self, guild: discord.Guild) -> discord.Embed:
        role = guild.self_role
        role_txt = role.mention if role else "my role"
        embed = discord.Embed(
            title="🛡️ Action required: move my role to the top",
            description=(
                f"Thanks for adding me! Discord only lets a bot moderate members whose highest role is "
                f"**below** the bot's role, and a bot can't move its own role.\n\n"
                f"**Server Owner / Bot Owner, please do this now:**\n"
                f"1. Open **Server Settings → Roles**.\n"
                f"2. Drag {role_txt} to the **very top** of the list (directly under the Server Owner).\n"
                f"3. Run `/hierarchy_check` to confirm, then `/setup` to create my log, quarantine and ticket channels.\n\n"
                f"Without this, bans/kicks, the anti-nuke freeze and rollback will silently fail against anyone "
                f"ranked above me."
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(name="Permissions I need", value=REQUIRED_PERMS, inline=False)
        return embed

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        embed = self._notice_embed(guild)
        channel = await self._find_private_channel(guild)
        if channel:
            await self.bot.db.set_config(guild.id, modlog_channel_id=channel.id)
            try:
                await channel.send(embed=embed)
                return
            except discord.HTTPException:
                pass
        # Fallbacks: DM the owner, then the system channel.
        if guild.owner:
            try:
                await guild.owner.send(f"I just joined **{guild.name}**.", embed=embed)
                return
            except discord.HTTPException:
                pass
        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            await guild.system_channel.send(embed=embed)

    # -------------------------------------------------------------- commands
    @app_commands.command(name="hierarchy_check", description="Check that my role is at the top of the role list.")
    @app_commands.guild_only()
    async def hierarchy_check(self, interaction: discord.Interaction) -> None:
        if hierarchy_ok(interaction.guild):
            await interaction.response.send_message("✅ My role is the highest in the server.", ephemeral=True)
        else:
            await interaction.response.send_message(embed=self._notice_embed(interaction.guild), ephemeral=True)

    @app_commands.command(name="setup", description="Create/repair log, quarantine and ticket infrastructure.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_cmd(self, interaction: discord.Interaction, mod_role: discord.Role | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, db = interaction.guild, self.bot.db
        cfg = await db.get_config(guild.id)
        made: list[str] = []

        async def role(key: str, name: str) -> discord.Role:
            r = guild.get_role(cfg.get(key) or 0)
            if r is None:
                r = await guild.create_role(name=name, permissions=discord.Permissions.none(), reason="ModBot setup")
                made.append(f"role {name}")
            return r

        async def text(key: str, name: str, overwrites=None) -> discord.TextChannel:
            c = guild.get_channel(cfg.get(key) or 0)
            if not isinstance(c, discord.TextChannel):
                c = await guild.create_text_channel(name, overwrites=overwrites or {}, reason="ModBot setup")
                made.append(f"#{name}")
            return c

        quarantine = await role("quarantine_role_id", "Quarantine")
        isolated = await role("isolation_role_id", "Isolated")
        joins = await text("joins_channel_id", "joins")
        dnt = await text("dnt_channel_id", "do-not-type")

        log_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, attach_files=True),
        }
        if mod_role:
            log_overwrites[mod_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        modlog = await text("modlog_channel_id", "mod-log", log_overwrites)

        await db.set_config(
            guild.id, quarantine_role_id=quarantine.id, isolation_role_id=isolated.id,
            joins_channel_id=joins.id, dnt_channel_id=dnt.id, modlog_channel_id=modlog.id,
            **({"mod_role_id": mod_role.id} if mod_role else {}),
        )
        q_cog = self.bot.get_cog("Quarantine")
        if q_cog:
            await q_cog.apply_role_locks(guild)

        lines = [f"Created: {', '.join(made) if made else 'nothing new'}.",
                 f"Mod log: {modlog.mention} · Joins: {joins.mention} · Do-not-type: {dnt.mention}"]
        if not hierarchy_ok(guild):
            lines.append("⚠️ My role is **not** at the top yet — see `/hierarchy_check`.")
        lines.append("Post the ticket panel with `/ticket_panel` in the channel of your choice.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Onboarding(bot))
