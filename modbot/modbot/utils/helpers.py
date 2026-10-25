"""Small shared helpers (trust checks, staff detection, mod-log lookup)."""
from __future__ import annotations

import discord

import config

# Permissions that make someone "staff" for the Inside-Man protocol.
STAFF_PERMS = (
    "administrator", "ban_members", "kick_members",
    "manage_roles", "manage_guild", "moderate_members",
)

MODLOG_NAMES = (
    "mod-log", "mod-logs", "modlog", "modlogs", "admin-only", "admin-log",
    "staff-log", "staff-only", "moderator-only", "mod-chat", "logs",
)


def is_trusted_id(bot, guild: discord.Guild, user_id: int) -> bool:
    """Guild owner, the bot itself and configured trusted IDs are never punished."""
    return user_id == guild.owner_id or user_id == bot.user.id or user_id in config.TRUSTED_IDS


def has_staff_perms(perms: discord.Permissions) -> bool:
    return any(getattr(perms, p) for p in STAFF_PERMS)


def is_staff_member(member: discord.Member) -> bool:
    """Human with moderator/admin powers (by permission or configured staff role). Bots are not 'staff'."""
    if member.bot:
        return False
    return has_staff_perms(member.guild_permissions) or any(r.id in config.STAFF_ROLE_IDS for r in member.roles)


def is_staff_role(role: discord.Role) -> bool:
    return role.id in config.STAFF_ROLE_IDS or has_staff_perms(role.permissions)


def hierarchy_ok(guild: discord.Guild) -> bool:
    """True when the bot's top role is the highest role in the server."""
    top = max(guild.roles, key=lambda r: r.position)
    return guild.me.top_role == top


async def get_modlog_channel(bot, guild: discord.Guild) -> discord.TextChannel | None:
    cfg = await bot.db.get_config(guild.id)
    ch = guild.get_channel(cfg.get("modlog_channel_id") or 0)
    if isinstance(ch, discord.TextChannel):
        return ch
    for name in MODLOG_NAMES:
        for ch in guild.text_channels:
            if ch.name == name and ch.permissions_for(guild.me).send_messages:
                return ch
    return None
