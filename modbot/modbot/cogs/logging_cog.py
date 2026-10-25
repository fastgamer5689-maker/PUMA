"""Photographic memory: every message, edit and deletion is written to SQLite.

`messages.content` always holds the ORIGINAL text; edits go to `message_edits`; deletes only
set `deleted_at`. DB triggers block DELETE, so wiping a channel (or deleting messages on
Discord) never removes the historical record.

Other cogs react to the custom events `logged_delete(row)` and `logged_edit(row, new_mention_ids)`.
"""
from __future__ import annotations

import json
import time

import discord
from discord import app_commands
from discord.ext import commands

INSERT_SQL = (
    "INSERT OR IGNORE INTO messages (message_id, guild_id, channel_id, channel_name, author_id, author_tag, "
    "is_bot, content, latest_content, attachments, mention_ids, mention_everyone, created_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


class MessageLogger(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, m: discord.Message) -> None:
        if m.guild is None:
            return
        mentions = sorted({u.id for u in m.mentions if not u.bot and u.id != m.author.id})
        await self.bot.db.execute(
            INSERT_SQL,
            (
                m.id, m.guild.id, m.channel.id, getattr(m.channel, "name", None), m.author.id, str(m.author),
                int(m.author.bot), m.content, m.content,
                json.dumps([a.url for a in m.attachments]), json.dumps(mentions),
                int("@everyone" in m.content or "@here" in m.content),
                m.created_at.timestamp(),
            ),
        )

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        data = payload.data
        if payload.guild_id is None or "content" not in data:
            return
        new = data["content"]
        db = self.bot.db
        row = await db.fetchone("SELECT * FROM messages WHERE message_id=?", (payload.message_id,))
        if row is None:  # message predates the bot: record what we can
            author = data.get("author")
            if author:
                await db.execute(
                    INSERT_SQL,
                    (payload.message_id, payload.guild_id, payload.channel_id, None, int(author["id"]),
                     author.get("username"), int(bool(author.get("bot"))), new, new, "[]", "[]", 0, time.time()),
                )
            return
        if row["latest_content"] == new:
            return
        now = time.time()
        await db.execute(
            "INSERT INTO message_edits (message_id, old_content, new_content, edited_at) VALUES (?,?,?,?)",
            (payload.message_id, row["latest_content"], new, now),
        )
        await db.execute("UPDATE messages SET latest_content=?, edited_at=? WHERE message_id=?",
                         (new, now, payload.message_id))
        new_mentions = sorted({int(u["id"]) for u in data.get("mentions", []) if not u.get("bot")})
        self.bot.dispatch("logged_edit", row, new_mentions)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        db = self.bot.db
        row = await db.fetchone("SELECT * FROM messages WHERE message_id=?", (payload.message_id,))
        if row is None:
            return
        await db.execute("UPDATE messages SET deleted_at=? WHERE message_id=? AND deleted_at IS NULL",
                         (time.time(), payload.message_id))
        self.bot.dispatch("logged_delete", row)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        now = time.time()
        await self.bot.db.executemany(
            "UPDATE messages SET deleted_at=? WHERE message_id=? AND deleted_at IS NULL",
            [(now, mid) for mid in payload.message_ids],
        )

    # ----------------------------------------------------------- inspection
    @app_commands.command(name="msglog", description="Show the original text and edit history of a logged message.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    async def msglog(self, interaction: discord.Interaction, message_id: str) -> None:
        if not message_id.isdigit():
            return await interaction.response.send_message("Give a numeric message ID.", ephemeral=True)
        db = self.bot.db
        row = await db.fetchone("SELECT * FROM messages WHERE message_id=? AND guild_id=?",
                                (int(message_id), interaction.guild.id))
        if row is None:
            return await interaction.response.send_message("Not in the log.", ephemeral=True)
        edits = await db.fetchall("SELECT * FROM message_edits WHERE message_id=? ORDER BY edited_at", (row["message_id"],))

        def stamp(ts: float | None) -> str:
            return f"<t:{int(ts)}:f>" if ts else "—"

        embed = discord.Embed(title=f"Message {row['message_id']}", color=discord.Color.blurple())
        embed.add_field(name="Author", value=f"<@{row['author_id']}> (`{row['author_id']}`)", inline=True)
        embed.add_field(name="Channel", value=f"<#{row['channel_id']}>", inline=True)
        embed.add_field(name="Sent", value=stamp(row["created_at"]), inline=True)
        embed.add_field(name="Original", value=(discord.utils.escape_markdown(row["content"] or "—"))[:1000], inline=False)
        if edits:
            hist = "\n".join(f"{stamp(e['edited_at'])}: {(e['new_content'] or '')[:150]}" for e in edits[-5:])
            embed.add_field(name=f"Edits ({len(edits)})", value=hist[:1000], inline=False)
        if row["deleted_at"]:
            embed.add_field(name="Deleted", value=stamp(row["deleted_at"]), inline=False)
        if row["attachments"] and row["attachments"] != "[]":
            embed.add_field(name="Attachments", value=row["attachments"][:1000], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MessageLogger(bot))
