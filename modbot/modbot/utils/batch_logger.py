"""Consolidated logging.

Callers add one *line* per action (e.g. one per banned user). Lines for the same
(guild, category) are collected and sent as ONE embed once things go quiet
(BATCH_QUIET_SECONDS) or when `flush()` is called explicitly (anti-nuke does this
when its sequence is finished). Long reports attach the full text as a file,
still inside the same single message.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass, field

import discord

import config
from utils.helpers import get_modlog_channel

log = logging.getLogger(__name__)
MAX_BODY = 3800
PREVIEW_LINES = 25


@dataclass
class _Batch:
    title: str
    color: discord.Color
    ping_mods: bool
    lines: list[str] = field(default_factory=list)
    first: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = None


class BatchLogger:
    def __init__(self, bot, quiet: float = config.BATCH_QUIET_SECONDS, max_wait: float = config.BATCH_MAX_WAIT_SECONDS):
        self.bot = bot
        self.quiet = quiet
        self.max_wait = max_wait
        self._batches: dict[tuple[int, str], _Batch] = {}

    def add(
        self,
        guild: discord.Guild,
        category: str,
        title: str,
        line: str,
        *,
        color: discord.Color = discord.Color.orange(),
        ping_mods: bool = False,
    ) -> None:
        key = (guild.id, category)
        batch = self._batches.get(key)
        if batch is None:
            batch = self._batches[key] = _Batch(title, color, ping_mods)
        batch.lines.append(line)
        batch.ping_mods = batch.ping_mods or ping_mods
        if batch.task:
            batch.task.cancel()
        delay = max(0.0, min(self.quiet, self.max_wait - (time.monotonic() - batch.first)))
        batch.task = asyncio.create_task(self._flush_later(key, delay))

    async def _flush_later(self, key: tuple[int, str], delay: float) -> None:
        await asyncio.sleep(delay)
        await self.flush(*key)

    async def flush(self, guild_id: int, category: str) -> None:
        batch = self._batches.pop((guild_id, category), None)
        if batch is None:
            return
        if batch.task and batch.task is not asyncio.current_task():
            batch.task.cancel()
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            await self._send(guild, batch)

    async def _send(self, guild: discord.Guild, batch: _Batch) -> None:
        count = len(batch.lines)
        body = "\n".join(batch.lines)
        file = None
        if len(body) > MAX_BODY:
            file = discord.File(io.BytesIO(body.encode()), filename="full_report.txt")
            body = "\n".join(batch.lines[:PREVIEW_LINES])[:MAX_BODY]
            body += f"\n… plus {max(0, count - PREVIEW_LINES)} more — see attached file."
        embed = discord.Embed(
            title=batch.title,
            description=f"**{count} action(s) bundled**\n{body}"[:4000],
            color=batch.color,
            timestamp=discord.utils.utcnow(),
        )
        channel = await get_modlog_channel(self.bot, guild)
        content = None
        if batch.ping_mods:
            cfg = await self.bot.db.get_config(guild.id)
            if cfg.get("mod_role_id"):
                content = f"<@&{cfg['mod_role_id']}>"
        try:
            if channel is not None:
                await channel.send(
                    content=content, embed=embed, file=file,
                    allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
                )
            elif guild.owner is not None:  # no log channel configured: tell the owner once
                await guild.owner.send(
                    content=f"(No mod-log channel in **{guild.name}** — run `/setup`.)", embed=embed, file=file
                )
        except discord.HTTPException:
            log.exception("Could not deliver batched log in guild %s", guild.id)
