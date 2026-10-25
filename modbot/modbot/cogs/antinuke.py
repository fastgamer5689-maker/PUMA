"""Anti-nuke: snapshot -> detect -> freeze -> purge -> rollback -> ONE summary report.

Flow when a threshold trips (rapid channel deletes/creates, or mass-ping spam):
  1. Incident opens; the server stays frozen until the owner runs /unfreeze.
  2. Freeze: @everyone loses Send Messages in every channel (previous state saved in `lockdown_state`).
  3. Purge: attackers (from the audit log) are banned. Staff who take part are NOT banned — the
     Inside-Man protocol strips their roles, isolates them, blacklists them and opens an owner-trial incident.
  4. Rollback: channels deleted during the attack are recreated from the snapshot (with the freeze
     applied); channels the attackers created are deleted.
  5. One consolidated report is posted to the mod log.

Requires: View Audit Log, Manage Channels, Manage Roles, Ban Members, Moderate Members — and the bot's
role above everyone it needs to act on.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections import defaultdict, deque
from contextlib import suppress
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils.helpers import is_staff_member, is_staff_role, is_trusted_id

log = logging.getLogger(__name__)

KIND_TEXT = {"delete": "rapid channel deletion", "create": "rapid channel creation", "ping": "mass-ping spam",
             "manual": "manual restore"}
LIMITS = {"delete": config.NUKE_DELETE_LIMIT, "create": config.NUKE_CREATE_LIMIT, "ping": config.MASS_PING_WINDOW_TOTAL}

UPSERT_SQL = (
    "INSERT INTO channel_snapshots (guild_id,channel_id,name,type,parent_id,position,topic,nsfw,slowmode,bitrate,"
    "user_limit,overwrites,created_at,snapshot_at,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(guild_id,channel_id) DO UPDATE SET name=excluded.name,type=excluded.type,parent_id=excluded.parent_id,"
    "position=excluded.position,topic=excluded.topic,nsfw=excluded.nsfw,slowmode=excluded.slowmode,"
    "bitrate=excluded.bitrate,user_limit=excluded.user_limit,overwrites=excluded.overwrites,"
    "snapshot_at=excluded.snapshot_at,deleted_at=NULL"
)


# --------------------------------------------------------------------------
# Snapshot (de)serialization helpers
# --------------------------------------------------------------------------
def serialize_overwrites(overwrites: dict) -> str:
    items = []
    for target, ow in overwrites.items():
        allow, deny = ow.pair()
        items.append({"id": target.id, "type": "role" if isinstance(target, discord.Role) else "member",
                      "allow": allow.value, "deny": deny.value})
    return json.dumps(sorted(items, key=lambda i: i["id"]))


def deserialize_overwrites(guild: discord.Guild, raw: str, freeze: bool = False) -> dict:
    out: dict = {}
    for o in json.loads(raw or "[]"):
        target = guild.get_role(o["id"]) if o["type"] == "role" else guild.get_member(o["id"])
        if target is not None:
            out[target] = discord.PermissionOverwrite.from_pair(discord.Permissions(o["allow"]),
                                                                 discord.Permissions(o["deny"]))
    if freeze:
        ow = out.get(guild.default_role, discord.PermissionOverwrite())
        ow.send_messages = False
        ow.send_messages_in_threads = False
        out[guild.default_role] = ow
    return out


def channel_row(ch: discord.abc.GuildChannel, created_by: int | None = None, overwrites_json: str | None = None) -> tuple:
    return (
        ch.guild.id, ch.id, ch.name, ch.type.value, getattr(ch, "category_id", None), ch.position,
        getattr(ch, "topic", None), int(bool(getattr(ch, "nsfw", False))), getattr(ch, "slowmode_delay", 0) or 0,
        getattr(ch, "bitrate", None), getattr(ch, "user_limit", None),
        overwrites_json if overwrites_json is not None else serialize_overwrites(ch.overwrites),
        ch.created_at.timestamp(), time.time(), created_by,
    )


def _tri(v: bool | None) -> int | None:
    return None if v is None else int(v)


def _untri(v: int | None) -> bool | None:
    return None if v is None else bool(v)


class Incident:
    def __init__(self, guild_id: int, kind: str, window_start: float):
        self.guild_id = guild_id
        self.kind = kind
        self.window_start = window_start
        self.started = time.time()
        self.attackers: set[int] = set()
        self.punished: set[int] = set()
        self.webhooks: set[int] = set()
        self.report: list[str] = []


class AuthKeyModal(discord.ui.Modal, title="Owner authorization"):
    key = discord.ui.TextInput(label="Owner authorization key", max_length=128)

    def __init__(self, cog: "AntiNuke", target: discord.Member):
        super().__init__()
        self.cog = cog
        self.target = target

    async def on_submit(self, interaction: discord.Interaction) -> None:
        expected = config.OWNER_AUTH_KEY
        if not expected or not hmac.compare_digest(self.key.value.encode(), expected.encode()):
            self.cog.bot.batch.add(interaction.guild, "authkey", "🔐 Failed owner-key attempt",
                                   f"{interaction.user.mention} tried to authorize {self.target.mention} with a wrong key.",
                                   color=discord.Color.red(), ping_mods=True)
            return await interaction.response.send_message("❌ Invalid key.", ephemeral=True)
        db = self.cog.bot.db
        await db.execute("DELETE FROM staff_blacklist WHERE guild_id=? AND user_id=?", (interaction.guild.id, self.target.id))
        await db.execute("UPDATE incidents SET status='owner_override' WHERE guild_id=? AND user_id=? AND status!='banned'",
                         (interaction.guild.id, self.target.id))
        await interaction.response.send_message(
            f"✅ {self.target.mention} may hold staff roles again. Roles must be re-granted manually.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())


def owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.guild is not None and (
            interaction.user.id == interaction.guild.owner_id or interaction.user.id in config.TRUSTED_IDS)
    return app_commands.check(predicate)


class AntiNuke(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.events: dict[tuple[int, str, int], deque] = defaultdict(deque)   # (guild, kind, actor) -> (ts, target, weight)
        self.active: dict[int, Incident] = {}
        self._audit_cache: dict[tuple[int, discord.AuditLogAction], dict[int, int]] = {}
        self._audit_locks: dict[tuple[int, discord.AuditLogAction], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._bot_deleted: set[int] = set()
        self._pending_webhooks: set[int] = set()

    async def cog_load(self) -> None:
        self.refresh_loop.start()

    async def cog_unload(self) -> None:
        self.refresh_loop.cancel()

    # ------------------------------------------------------------ snapshots
    async def snapshot_guild(self, guild: discord.Guild) -> None:
        rows = [channel_row(ch) for ch in guild.channels]
        await self.bot.db.executemany(UPSERT_SQL, rows)
        live = {ch.id for ch in guild.channels}
        known = await self.bot.db.fetchall(
            "SELECT channel_id FROM channel_snapshots WHERE guild_id=? AND deleted_at IS NULL", (guild.id,))
        gone = [(time.time(), guild.id, r["channel_id"]) for r in known if r["channel_id"] not in live]
        if gone:
            await self.bot.db.executemany(
                "UPDATE channel_snapshots SET deleted_at=? WHERE guild_id=? AND channel_id=?", gone)

    @tasks.loop(minutes=10)
    async def refresh_loop(self) -> None:
        for guild in self.bot.guilds:
            if guild.id not in self.active:  # never overwrite the clean snapshot mid-incident
                try:
                    await self.snapshot_guild(guild)
                except Exception:
                    log.exception("Snapshot failed for %s", guild.id)
        await self.bot.db.execute(
            "DELETE FROM channel_snapshots WHERE deleted_at IS NOT NULL AND deleted_at < ?", (time.time() - 86400,))

    @refresh_loop.before_loop
    async def _before_refresh(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.snapshot_guild(guild)

    # ---------------------------------------------------------- attribution
    async def _attribute(self, guild: discord.Guild, action: discord.AuditLogAction, target_id: int) -> int | None:
        """Who did it? Cached audit-log lookup (one fetch serves a whole burst of events)."""
        if not guild.me.guild_permissions.view_audit_log:
            return None
        key = (guild.id, action)
        for attempt in range(2):
            cache = self._audit_cache.setdefault(key, {})
            if target_id in cache:
                return cache[target_id]
            async with self._audit_locks[key]:
                if target_id not in cache:
                    try:
                        async for entry in guild.audit_logs(limit=25, action=action):
                            if entry.target is not None and time.time() - entry.created_at.timestamp() < 30:
                                cache[entry.target.id] = entry.user_id
                    except discord.HTTPException:
                        return None
                    if len(cache) > 500:
                        cache.clear()
            if target_id in cache:
                return cache[target_id]
            await asyncio.sleep(1)  # audit log can lag a moment behind the gateway event
        return None

    # ---------------------------------------------------------- event intake
    def _record(self, guild: discord.Guild, kind: str, executor_id: int | None, target_id: int, weight: int = 1) -> None:
        if executor_id and is_trusted_id(self.bot, guild, executor_id):
            return
        now = time.time()
        dq = self.events[(guild.id, kind, executor_id or 0)]
        dq.append((now, target_id, weight))
        while dq and now - dq[0][0] > config.NUKE_WINDOW_SECONDS:
            dq.popleft()
        total = sum(w for _, _, w in dq)
        if total < LIMITS[kind]:
            return
        inc = self.active.get(guild.id)
        if inc is not None:  # already frozen: only act on actors who themselves crossed the limit
            if executor_id and executor_id not in inc.punished:
                inc.attackers.add(executor_id)
                asyncio.create_task(self._punish(guild, executor_id, inc))
            return
        self._trigger(guild, kind, executor_id, dq[0][0])

    def _participants(self, guild: discord.Guild, since: float) -> set[int]:
        out: set[int] = set()
        for (gid, kind, actor), dq in self.events.items():
            if gid != guild.id or not actor or is_trusted_id(self.bot, guild, actor):
                continue
            recent = [(ts, w) for ts, _, w in dq if ts >= since]
            need = config.PARTICIPANT_MIN_PINGS if kind == "ping" else config.PARTICIPANT_MIN_EVENTS
            if sum(w for _, w in recent) >= need:
                out.add(actor)
        return out

    def _trigger(self, guild: discord.Guild, kind: str, executor_id: int | None, window_start: float) -> None:
        inc = Incident(guild.id, kind, window_start)
        if executor_id:
            inc.attackers.add(executor_id)
        inc.attackers |= self._participants(guild, window_start)
        inc.webhooks |= self._pending_webhooks
        self._pending_webhooks.clear()
        self.active[guild.id] = inc
        log.warning("ANTI-NUKE triggered in %s (%s) attackers=%s", guild.id, kind, inc.attackers)
        asyncio.create_task(self._respond(guild, inc))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild
        if channel.id in self._bot_deleted:
            self._bot_deleted.discard(channel.id)
            return
        await self.bot.db.execute(
            "UPDATE channel_snapshots SET deleted_at=? WHERE guild_id=? AND channel_id=? AND deleted_at IS NULL",
            (time.time(), guild.id, channel.id))
        executor = await self._attribute(guild, discord.AuditLogAction.channel_delete, channel.id)
        if executor and is_trusted_id(self.bot, guild, executor):  # legit deletion: forget the channel
            await self.bot.db.execute("DELETE FROM channel_snapshots WHERE guild_id=? AND channel_id=?",
                                      (guild.id, channel.id))
            return
        self._record(guild, "delete", executor, channel.id)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild
        executor = await self._attribute(guild, discord.AuditLogAction.channel_create, channel.id)
        inc = self.active.get(guild.id)
        if inc and executor and executor in inc.attackers and not is_trusted_id(self.bot, guild, executor):
            self._bot_deleted.add(channel.id)  # attacker still creating spam channels: delete on sight
            with suppress(discord.HTTPException):
                await channel.delete(reason="Anti-nuke: channel created by an attacker")
            return
        await self.bot.db.execute(UPSERT_SQL, channel_row(channel, created_by=executor))
        self._record(guild, "create", executor, channel.id)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        if after.guild.id not in self.active:
            await self.bot.db.execute(UPSERT_SQL, channel_row(after))

    @commands.Cog.listener()
    async def on_message(self, m: discord.Message) -> None:
        if m.guild is None or m.author.id == self.bot.user.id:
            return
        if not m.webhook_id and is_trusted_id(self.bot, m.guild, m.author.id):
            return
        pings = len(set(m.raw_mentions)) + len(set(m.raw_role_mentions))
        pings += 10 * (m.content.count("@everyone") + m.content.count("@here"))
        if pings < 3:
            return
        actor = m.webhook_id or m.author.id
        inc = self.active.get(m.guild.id)
        if m.webhook_id and inc:
            inc.webhooks.add(m.webhook_id)
        if pings >= config.MASS_PING_PER_MESSAGE:
            with suppress(discord.HTTPException):
                await m.delete()
            if m.guild.id not in self.active:
                if m.webhook_id:
                    # register the webhook BEFORE the incident starts so _punish deletes it instead of banning
                    self._pending_webhooks.add(m.webhook_id)
                self._trigger(m.guild, "ping", actor, time.time())
                return
        self._record(m.guild, "ping", actor, m.id, weight=pings)

    # -------------------------------------------------------------- response
    async def _respond(self, guild: discord.Guild, inc: Incident) -> None:
        try:
            who = ", ".join(f"<@{a}>" for a in inc.attackers) or "unknown (audit log unavailable?)"
            inc.report.append(f"**Trigger:** {KIND_TEXT.get(inc.kind, inc.kind)} — suspected: {who}")
            await asyncio.gather(self._freeze(guild, inc), self._purge(guild, inc))
            await self._rollback(guild, inc)
            await asyncio.sleep(5)  # catch stragglers still in flight
            await self._rollback(guild, inc)
            inc.report.append("🧊 Server remains **frozen**. Owner: review, then run `/unfreeze`.")
        except Exception:
            log.exception("Anti-nuke response failed")
            inc.report.append("❌ The response hit an unexpected error — check the bot logs and act manually.")
        finally:
            for line in inc.report:  # everything bundled into ONE message
                self.bot.batch.add(guild, "antinuke", "🚨 Anti-Nuke Incident Report", line,
                                   color=discord.Color.dark_red(), ping_mods=True)
            await self.bot.batch.flush(guild.id, "antinuke")

    async def _freeze(self, guild: discord.Guild, inc: Incident) -> None:
        default, sem = guild.default_role, asyncio.Semaphore(8)
        ok = failed = 0

        async def lock(ch: discord.abc.GuildChannel) -> None:
            nonlocal ok, failed
            async with sem:
                try:
                    ow = ch.overwrites_for(default)
                    await self.bot.db.execute(
                        "INSERT OR IGNORE INTO lockdown_state (guild_id,channel_id,prev_send,prev_threads) VALUES (?,?,?,?)",
                        (guild.id, ch.id, _tri(ow.send_messages), _tri(ow.send_messages_in_threads)))
                    ow.send_messages = False
                    ow.send_messages_in_threads = False
                    await ch.set_permissions(default, overwrite=ow, reason="Anti-nuke emergency freeze")
                    ok += 1
                except discord.HTTPException:
                    failed += 1

        await asyncio.gather(*(lock(c) for c in guild.channels))
        inc.report.append(f"🧊 Freeze: @everyone can't send messages in **{ok}** channels"
                          + (f" ({failed} failed)" if failed else "") + ".")

    async def _purge(self, guild: discord.Guild, inc: Incident) -> None:
        for uid in list(inc.attackers):
            await self._punish(guild, uid, inc)

    async def _punish(self, guild: discord.Guild, uid: int, inc: Incident) -> None:
        if uid in inc.punished or is_trusted_id(self.bot, guild, uid):
            return
        inc.punished.add(uid)
        if uid in inc.webhooks:
            with suppress(discord.HTTPException):
                wh = await self.bot.fetch_webhook(uid)
                await wh.delete(reason="Anti-nuke: webhook used in a raid")
                inc.report.append(f"🪝 Deleted raid webhook `{uid}`.")
            return
        member = guild.get_member(uid)
        if member is None:
            with suppress(discord.HTTPException):
                member = await guild.fetch_member(uid)
        if member is not None and is_staff_member(member):
            return await self._inside_man(guild, member, inc)
        label = "bot" if member is not None and member.bot else "account"
        try:
            await guild.ban(discord.Object(id=uid), reason="Anti-nuke: participant in a raid/nuke",
                            delete_message_seconds=3600)
            inc.report.append(f"🔨 Banned {label} <@{uid}> (`{uid}`).")
        except discord.HTTPException as exc:
            inc.report.append(f"⚠️ Could NOT ban {label} <@{uid}> (`{uid}`, HTTP {exc.status}) — it likely outranks me. "
                              f"Fix `/hierarchy_check` and remove it manually.")

    async def _inside_man(self, guild: discord.Guild, member: discord.Member, inc: Incident) -> None:
        """A moderator/admin took part: strip, isolate, blacklist, flag for an owner trial. No ban."""
        cfg = await self.bot.db.get_config(guild.id)
        iso = guild.get_role(cfg.get("isolation_role_id") or 0)
        top = guild.me.top_role
        keep = [r for r in member.roles if not r.is_default() and (r.managed or r >= top)]
        removed = [r for r in member.roles if not r.is_default() and r not in keep]
        new_roles = keep + ([iso] if iso and iso not in keep else [])
        problems = []
        try:
            await member.edit(roles=new_roles, reason="Anti-nuke: inside-man protocol")
        except discord.HTTPException as exc:
            problems.append(f"role strip failed (HTTP {exc.status})")
        with suppress(discord.HTTPException):
            await member.timeout(timedelta(days=28), reason="Anti-nuke: inside-man isolation")
        unremovable = [r.name for r in keep if not r.managed]
        if unremovable:
            problems.append("still holds roles above mine: " + ", ".join(unremovable))

        db = self.bot.db
        await db.execute("INSERT OR REPLACE INTO staff_blacklist (guild_id,user_id,reason,created_at) VALUES (?,?,?,?)",
                         (guild.id, member.id, f"Inside man during {inc.kind} incident", time.time()))
        iid = await db.execute(
            "INSERT INTO incidents (guild_id,user_id,kind,details,created_at) VALUES (?,?,?,?,?)",
            (guild.id, member.id, "inside_man",
             json.dumps({"trigger": inc.kind, "removed_roles": [r.name for r in removed]}), time.time()))
        inc.report.append(
            f"🕵️ **INSIDE MAN:** {member.mention} (`{member.id}`) — {len(removed)} role(s) stripped, isolated + timed out, "
            f"**permanently blacklisted** from staff roles. Incident **#{iid}** awaits owner trial."
            + (f" ⚠️ {'; '.join(problems)}" if problems else ""))
        if guild.owner:
            with suppress(discord.HTTPException):
                await guild.owner.send(f"🕵️ Inside-man incident #{iid} in **{guild.name}**: {member} ({member.id}) "
                                       f"took part in a {KIND_TEXT.get(inc.kind, inc.kind)}. Use `/incidents` and `/trial_resolve`.")

    # -------------------------------------------------------------- rollback
    async def _rollback(self, guild: discord.Guild, inc: Incident) -> list[str]:
        db, lines = self.bot.db, []
        att = [a for a in inc.attackers]
        since = inc.window_start - 5
        marks = ",".join("?" for _ in att)

        # 1) delete channels created by attackers during the attack
        if att:
            spam = await db.fetchall(
                f"SELECT channel_id,name FROM channel_snapshots WHERE guild_id=? AND created_by IN ({marks}) "
                f"AND deleted_at IS NULL AND created_at>=?", (guild.id, *att, since))
            names = []
            for r in spam:
                ch = guild.get_channel(r["channel_id"])
                if ch is not None:
                    self._bot_deleted.add(ch.id)
                    with suppress(discord.HTTPException):
                        await ch.delete(reason="Anti-nuke rollback: spam channel")
                        names.append(r["name"])
                await db.execute("DELETE FROM channel_snapshots WHERE guild_id=? AND channel_id=?", (guild.id, r["channel_id"]))
            if names:
                lines.append(f"🧹 Deleted **{len(names)}** spam channel(s): {', '.join(names)[:500]}")

        # 2) recreate channels deleted during the attack
        where, params = "guild_id=? AND deleted_at IS NOT NULL AND deleted_at>=?", [guild.id, since]
        if att:
            where += f" AND (created_by IS NULL OR created_by NOT IN ({marks}))"
            params += att
        rows = [r for r in await db.fetchall(f"SELECT * FROM channel_snapshots WHERE {where}", params)
                if guild.get_channel(r["channel_id"]) is None]
        rows.sort(key=lambda r: (r["type"] != discord.ChannelType.category.value, r["position"] or 0))
        parent_map: dict[int, int] = {}
        restored, failed = [], []
        for r in rows:
            try:
                new = await self._recreate(guild, r, parent_map)
                parent_map[r["channel_id"]] = new.id
                restored.append(r["name"])
            except discord.HTTPException as exc:
                failed.append(f"{r['name']} (HTTP {exc.status})")
        if restored:
            lines.append(f"♻️ Recreated **{len(restored)}** channel(s) with original overwrites: {', '.join(restored)[:600]}")
        if failed:
            lines.append(f"❌ Could not recreate: {', '.join(failed)[:500]}")
        inc.report.extend(lines)
        return lines

    async def _recreate(self, guild: discord.Guild, r: dict, parent_map: dict[int, int]) -> discord.abc.GuildChannel:
        db = self.bot.db
        overwrites = deserialize_overwrites(guild, r["overwrites"], freeze=True)  # stay frozen until /unfreeze
        parent_id = parent_map.get(r["parent_id"], r["parent_id"])
        category = guild.get_channel(parent_id) if parent_id else None
        category = category if isinstance(category, discord.CategoryChannel) else None
        t = discord.ChannelType(r["type"])
        common = {"name": r["name"], "overwrites": overwrites, "reason": "Anti-nuke rollback"}

        if t is discord.ChannelType.category:
            new = await guild.create_category(position=r["position"] or 0, **common)
        elif t is discord.ChannelType.voice or t is discord.ChannelType.stage_voice:
            kw = {"category": category, "position": r["position"] or 0, **common}
            if t is discord.ChannelType.voice:
                if r["bitrate"]:
                    kw["bitrate"] = min(r["bitrate"], int(guild.bitrate_limit))
                if r["user_limit"]:
                    kw["user_limit"] = r["user_limit"]
                new = await guild.create_voice_channel(**kw)
            else:
                new = await guild.create_stage_channel(**kw)
        elif t is discord.ChannelType.forum:
            new = await guild.create_forum(topic=r["topic"], category=category, position=r["position"] or 0,
                                           nsfw=bool(r["nsfw"]), **common)
        else:  # text / news
            new = await guild.create_text_channel(topic=r["topic"], nsfw=bool(r["nsfw"]), slowmode_delay=r["slowmode"] or 0,
                                                  category=category, position=r["position"] or 0, **common)

        # re-key the snapshot under the new channel ID, storing the ORIGINAL (unfrozen) overwrites
        await db.execute("DELETE FROM channel_snapshots WHERE guild_id=? AND channel_id=?", (guild.id, r["channel_id"]))
        await db.execute(UPSERT_SQL, channel_row(new, overwrites_json=r["overwrites"]))
        await db.execute("UPDATE channel_snapshots SET parent_id=? WHERE guild_id=? AND parent_id=?",
                         (new.id, guild.id, r["channel_id"]))
        original = deserialize_overwrites(guild, r["overwrites"]).get(guild.default_role, discord.PermissionOverwrite())
        await db.execute(
            "INSERT OR REPLACE INTO lockdown_state (guild_id,channel_id,prev_send,prev_threads) VALUES (?,?,?,?)",
            (guild.id, new.id, _tri(original.send_messages), _tri(original.send_messages_in_threads)))
        return new

    # ----------------------------------------------- staff blacklist enforcing
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        added = set(after.roles) - set(before.roles)
        if not added:
            return
        row = await self.bot.db.fetchone("SELECT 1 FROM staff_blacklist WHERE guild_id=? AND user_id=?",
                                         (after.guild.id, after.id))
        if not row:
            return
        bad = [r for r in added if is_staff_role(r)]
        if not bad:
            return
        try:
            await after.remove_roles(*bad, reason="Permanent staff blacklist (inside-man protocol)")
            line = f"🚫 Blocked blacklisted user {after.mention} from receiving {', '.join(r.name for r in bad)}."
        except discord.HTTPException:
            line = f"⚠️ Blacklisted user {after.mention} was given {', '.join(r.name for r in bad)} and I could not remove it!"
        self.bot.batch.add(after.guild, "blacklist", "🚫 Staff blacklist enforcement", line,
                           color=discord.Color.red(), ping_mods=True)

    # -------------------------------------------------------------- commands
    @app_commands.command(name="unfreeze", description="End the lockdown and restore @everyone's original permissions.")
    @app_commands.guild_only()
    @owner_only()
    async def unfreeze(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, default = interaction.guild, interaction.guild.default_role
        rows = await self.bot.db.fetchall("SELECT * FROM lockdown_state WHERE guild_id=?", (guild.id,))
        fixed = 0
        for r in rows:
            ch = guild.get_channel(r["channel_id"])
            if ch is None:
                continue
            ow = ch.overwrites_for(default)
            ow.send_messages = _untri(r["prev_send"])
            ow.send_messages_in_threads = _untri(r["prev_threads"])
            with suppress(discord.HTTPException):
                await ch.set_permissions(default, overwrite=None if ow.is_empty() else ow, reason="Anti-nuke unfreeze")
                fixed += 1
        await self.bot.db.execute("DELETE FROM lockdown_state WHERE guild_id=?", (guild.id,))
        self.active.pop(guild.id, None)
        for k in [k for k in self.events if k[0] == guild.id]:
            del self.events[k]
        await self.snapshot_guild(guild)
        await interaction.followup.send(f"🔓 Unfroze {fixed} channel(s); snapshot refreshed.", ephemeral=True)

    @app_commands.command(name="snapshot", description="Force a fresh server-structure snapshot.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def snapshot_cmd(self, interaction: discord.Interaction) -> None:
        if interaction.guild.id in self.active:
            return await interaction.response.send_message("Incident active — snapshots are paused.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.snapshot_guild(interaction.guild)
        await interaction.followup.send("📸 Snapshot saved.", ephemeral=True)

    @app_commands.command(name="restore_channels", description="Manually recreate channels deleted in the last N minutes.")
    @app_commands.guild_only()
    @owner_only()
    async def restore_channels(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 1440] = 15) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        inc = Incident(interaction.guild.id, "manual", time.time() - minutes * 60)
        lines = await self._rollback(interaction.guild, inc)
        await interaction.followup.send("\n".join(lines) or "Nothing to restore.", ephemeral=True)

    @app_commands.command(name="incidents", description="List inside-man incidents awaiting an owner trial.")
    @app_commands.guild_only()
    @owner_only()
    async def incidents(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM incidents WHERE guild_id=? AND status='pending_owner_trial' ORDER BY id DESC LIMIT 20",
            (interaction.guild.id,))
        text = "\n".join(f"**#{r['id']}** <@{r['user_id']}> — {r['kind']} — <t:{int(r['created_at'])}:R>" for r in rows)
        await interaction.response.send_message(text or "No pending trials.", ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="trial_resolve", description="Owner verdict on an inside-man incident.")
    @app_commands.guild_only()
    @owner_only()
    @app_commands.choices(verdict=[app_commands.Choice(name="ban", value="ban"),
                                   app_commands.Choice(name="release isolation (blacklist stays)", value="release")])
    async def trial_resolve(self, interaction: discord.Interaction, incident_id: int, verdict: app_commands.Choice[str]) -> None:
        guild, db = interaction.guild, self.bot.db
        row = await db.fetchone("SELECT * FROM incidents WHERE id=? AND guild_id=?", (incident_id, guild.id))
        if row is None:
            return await interaction.response.send_message("Unknown incident.", ephemeral=True)
        if verdict.value == "ban":
            await guild.ban(discord.Object(id=row["user_id"]), reason=f"Owner trial verdict, incident #{incident_id}")
            status, msg = "banned", "🔨 Banned."
        else:
            member = guild.get_member(row["user_id"])
            cfg = await db.get_config(guild.id)
            iso = guild.get_role(cfg.get("isolation_role_id") or 0)
            if member:
                with suppress(discord.HTTPException):
                    if iso:
                        await member.remove_roles(iso)
                    await member.timeout(None)
            status, msg = "released", "Isolation lifted. They remain permanently blacklisted from staff roles."
        await db.execute("UPDATE incidents SET status=? WHERE id=?", (status, incident_id))
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="authorize_staff", description="Owner override: allow a blacklisted user to hold staff roles again.")
    @app_commands.guild_only()
    @owner_only()
    async def authorize_staff(self, interaction: discord.Interaction, user: discord.Member) -> None:
        await interaction.response.send_modal(AuthKeyModal(self, user))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntiNuke(bot))
