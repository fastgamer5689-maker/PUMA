"""Autonomous ticket service.

Members open a private ticket from a persistent panel, pick an issue type and the bot resolves what it can on its own:
  * quarantine  - explains/auto-releases from vetting
  * record      - shows the member's own warnings
  * faq / other - matches the server FAQ (/faq_add), then falls back to the AI module
  * report      - forwards the report to the moderators in ONE batched log entry
Humans only get involved when the member presses "Need a human" (or files a report).
"""
from __future__ import annotations

import io
import logging
import re
import time
from contextlib import suppress

import discord
from discord import app_commands
from discord.ext import commands

from utils.helpers import get_modlog_channel, is_staff_member

log = logging.getLogger(__name__)

ISSUES = {
    "quarantine": ("Stuck in new-member vetting", "⏳"),
    "record": ("Check my warnings / record", "📋"),
    "faq": ("Question about the server", "❓"),
    "report": ("Report a user or message", "🚩"),
    "other": ("Something else", "💬"),
}


class IssueSelect(discord.ui.Select):
    def __init__(self, cog: "Tickets"):
        super().__init__(placeholder="Pick the closest match…",
                         options=[discord.SelectOption(label=l, value=k, emoji=e) for k, (l, e) in ISSUES.items()])
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        ch = await self.cog.create_ticket(interaction.user, self.values[0])
        await interaction.followup.send(f"🎫 Your ticket is ready: {ch.mention}", ephemeral=True)


class IssueSelectView(discord.ui.View):
    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=120)
        self.add_item(IssueSelect(cog))


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Open a ticket", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="modbot:ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        row = await self.cog.bot.db.fetchone(
            "SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'",
            (interaction.guild.id, interaction.user.id))
        if row and interaction.guild.get_channel(row["channel_id"]):
            return await interaction.response.send_message(
                f"You already have an open ticket: <#{row['channel_id']}>", ephemeral=True)
        await interaction.response.send_message("What do you need help with?", view=IssueSelectView(self.cog), ephemeral=True)


class TicketControlView(discord.ui.View):
    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Solved — close", emoji="✅", style=discord.ButtonStyle.success, custom_id="modbot:ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        t = self.cog.tickets.get(interaction.channel.id)
        if t is None:
            return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
        if interaction.user.id != t["user_id"] and not is_staff_member(interaction.user):
            return await interaction.response.send_message("Only the ticket owner or staff can close this.", ephemeral=True)
        await interaction.response.send_message("Closing in 5 seconds… a transcript is saved for the moderators.")
        await self.cog.close_ticket(interaction.channel, str(interaction.user))

    @discord.ui.button(label="Need a human", emoji="🙋", style=discord.ButtonStyle.danger, custom_id="modbot:ticket:human")
    async def human(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        t = self.cog.tickets.get(interaction.channel.id)
        if t is None or interaction.user.id != t["user_id"]:
            return await interaction.response.send_message("Only the ticket owner can request a human.", ephemeral=True)
        await self.cog.escalate(interaction.channel, interaction.user, "requested a human")
        await interaction.response.send_message("🙋 A moderator has been called. Please describe your issue while you wait.")


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tickets: dict[int, dict] = {}  # channel_id -> {user_id, issue, escalated, reported}

    async def cog_load(self) -> None:
        rows = await self.bot.db.fetchall("SELECT * FROM tickets WHERE status='open'")
        self.tickets = {r["channel_id"]: dict(r) for r in rows}
        self.bot.add_view(TicketPanelView(self))
        self.bot.add_view(TicketControlView(self))

    # ------------------------------------------------------------- lifecycle
    async def create_ticket(self, member: discord.Member, issue: str) -> discord.TextChannel:
        guild, db = member.guild, self.bot.db
        cfg = await db.get_config(guild.id)
        category = guild.get_channel(cfg.get("ticket_category_id") or 0)
        if not isinstance(category, discord.CategoryChannel):
            category = await guild.create_category("Tickets", reason="Ticket system")
            await db.set_config(guild.id, ticket_category_id=category.id)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                attach_files=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                  manage_channels=True, embed_links=True),
        }
        mod_role = guild.get_role(cfg.get("mod_role_id") or 0)
        if mod_role:
            overwrites[mod_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                                read_message_history=True)
        slug = re.sub(r"[^a-z0-9]+", "-", member.name.lower()).strip("-") or "user"
        ch = await guild.create_text_channel(f"ticket-{slug}", category=category, overwrites=overwrites,
                                             topic=f"Ticket for {member} ({member.id}) — {issue}",
                                             reason=f"Ticket opened by {member}")
        await db.execute("INSERT INTO tickets (guild_id,channel_id,user_id,issue,created_at) VALUES (?,?,?,?,?)",
                         (guild.id, ch.id, member.id, issue, time.time()))
        self.tickets[ch.id] = {"user_id": member.id, "issue": issue, "escalated": 0, "reported": 0}

        label, emoji = ISSUES[issue]
        embed = discord.Embed(title=f"{emoji} {label}", color=discord.Color.blurple(),
                              description=f"Hi {member.mention}! I'll try to sort this out right away. "
                                          f"Press **Need a human** at any time, or **Solved — close** when done.")
        await ch.send(embed=embed, view=TicketControlView(self))
        await self._start_flow(ch, member, issue)
        return ch

    async def _start_flow(self, ch: discord.TextChannel, member: discord.Member, issue: str) -> None:
        guild, db = member.guild, self.bot.db
        if issue == "quarantine":
            row = await db.fetchone("SELECT * FROM quarantine WHERE guild_id=? AND user_id=?", (guild.id, member.id))
            if not row or row["released"] or row["is_bot"]:
                await ch.send("You're not currently in new-member vetting. If you still can't see channels, "
                              "press **Need a human**.")
            elif row["release_at"] <= time.time():
                await self.bot.get_cog("Quarantine").release_member(guild, member.id)
                await ch.send("✅ Your vetting period was over — I've released you. Welcome!")
            else:
                mins = max(1, int((row["release_at"] - time.time()) / 60) + 1)
                await ch.send(f"New members are held for a short vetting period to keep the server safe. "
                              f"You'll be released automatically in about **{mins} minute(s)**. Nothing else to do!")
        elif issue == "record":
            rows = await db.fetchall(
                "SELECT kind, COUNT(*) n FROM infractions WHERE guild_id=? AND user_id=? AND ts>=? GROUP BY kind",
                (guild.id, member.id, time.time() - 30 * 86400))
            text = "\n".join(f"• `{r['kind']}` × {r['n']}" for r in rows) or "Your record is clean for the last 30 days. 🎉"
            await ch.send(f"**Your last 30 days:**\n{text}")
        elif issue == "report":
            await ch.send("Tell me who or what you're reporting (user, message link, what happened). "
                          "I'll pass it straight to the moderators.")
        else:
            await ch.send("Type your question and I'll check the server FAQ" +
                          (" and ask my AI helper." if self.bot.get_cog("AIChat") and self.bot.get_cog("AIChat").enabled else "."))

    async def escalate(self, ch: discord.TextChannel, member: discord.Member, why: str) -> None:
        t = self.tickets.get(ch.id)
        if t:
            t["escalated"] = 1
        await self.bot.db.execute("UPDATE tickets SET escalated=1 WHERE channel_id=?", (ch.id,))
        cfg = await self.bot.db.get_config(ch.guild.id)
        if cfg.get("mod_role_id"):
            with suppress(discord.HTTPException):
                await ch.send(f"<@&{cfg['mod_role_id']}>", allowed_mentions=discord.AllowedMentions(roles=True))
        self.bot.batch.add(ch.guild, "tickets", "🎫 Tickets needing a human",
                           f"🙋 {member.mention} {why} in {ch.mention} ({ISSUES[t['issue']][0] if t else 'ticket'}).",
                           color=discord.Color.orange())

    async def close_ticket(self, ch: discord.TextChannel, closed_by: str) -> None:
        import asyncio
        rows = await self.bot.db.fetchall(
            "SELECT author_tag, COALESCE(content,'') c, created_at FROM messages WHERE channel_id=? ORDER BY created_at",
            (ch.id,))
        transcript = "\n".join(f"[{time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['created_at']))}] {r['author_tag']}: {r['c']}"
                               for r in rows) or "(empty)"
        await self.bot.db.execute("UPDATE tickets SET status='closed', closed_at=? WHERE channel_id=?", (time.time(), ch.id))
        t = self.tickets.pop(ch.id, None)
        log_ch = await get_modlog_channel(self.bot, ch.guild)
        if log_ch:
            with suppress(discord.HTTPException):
                await log_ch.send(f"🎫 Ticket `{ch.name}` closed by {closed_by}"
                                  f"{' (was escalated)' if t and t['escalated'] else ''}.",
                                  file=discord.File(io.BytesIO(transcript.encode()), filename=f"{ch.name}.txt"),
                                  allowed_mentions=discord.AllowedMentions.none())
        await asyncio.sleep(5)
        with suppress(discord.HTTPException):
            await ch.delete(reason="Ticket closed")

    # --------------------------------------------------------------- answers
    async def _faq_answer(self, guild_id: int, text: str) -> str | None:
        rows = await self.bot.db.fetchall("SELECT * FROM faq WHERE guild_id=?", (guild_id,))
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        best, score = None, 0
        for r in rows:
            keys = {k.strip().lower() for k in (r["keywords"] or "").split(",") if k.strip()}
            keys |= {w for w in re.findall(r"[a-z0-9]+", r["question"].lower()) if len(w) > 3}
            s = len(words & keys)
            if s > score:
                best, score = r, s
        return best["answer"] if best and score else None

    @commands.Cog.listener()
    async def on_message(self, m: discord.Message) -> None:
        t = self.tickets.get(m.channel.id)
        if t is None or m.author.bot or m.author.id != t["user_id"] or t["escalated"]:
            return
        if t["issue"] == "report":
            if t["reported"]:
                return
            t["reported"] = 1
            await self.bot.db.execute("UPDATE tickets SET reported=1 WHERE channel_id=?", (m.channel.id,))
            self.bot.batch.add(m.guild, "tickets", "🎫 Tickets needing a human",
                               f"🚩 **Report** from {m.author.mention} in {m.channel.mention}: "
                               f"{discord.utils.escape_mentions(m.content)[:500]}",
                               color=discord.Color.red(), ping_mods=True)
            await self.escalate(m.channel, m.author, "filed a report")
            return await m.channel.send("Thanks — your report was forwarded to the moderators. They'll follow up here.")

        if t["issue"] in ("faq", "other"):
            answer = await self._faq_answer(m.guild.id, m.content)
            if answer is None:
                ai = self.bot.get_cog("AIChat")
                if ai:
                    answer = await ai.respond(f"ticket:{m.channel.id}", m.author.display_name, m.content,
                                              "You are answering inside a support ticket. If you can't resolve it, "
                                              "tell the member to press 'Need a human'.")
            await m.channel.send(answer or "I couldn't find an answer for that — press **Need a human** and a moderator will help.",
                                 allowed_mentions=discord.AllowedMentions.none())

    # -------------------------------------------------------------- commands
    @app_commands.command(name="ticket_panel", description="Post the ticket panel in this channel.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_panel(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="🎫 Need help?", color=discord.Color.blurple(),
                              description="Open a private ticket. Most common issues are solved instantly by the bot; "
                                          "a human steps in only when needed.")
        await interaction.channel.send(embed=embed, view=TicketPanelView(self))
        await interaction.response.send_message("Panel posted.", ephemeral=True)

    @app_commands.command(name="faq_add", description="Add a FAQ entry the ticket bot can answer.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def faq_add(self, interaction: discord.Interaction, question: str, answer: str, keywords: str = "") -> None:
        fid = await self.bot.db.execute("INSERT INTO faq (guild_id,question,answer,keywords) VALUES (?,?,?,?)",
                                        (interaction.guild.id, question, answer, keywords))
        await interaction.response.send_message(f"FAQ #{fid} added.", ephemeral=True)

    @app_commands.command(name="faq_remove", description="Remove a FAQ entry.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def faq_remove(self, interaction: discord.Interaction, faq_id: int) -> None:
        await self.bot.db.execute("DELETE FROM faq WHERE id=? AND guild_id=?", (faq_id, interaction.guild.id))
        await interaction.response.send_message("Removed (if it existed).", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
