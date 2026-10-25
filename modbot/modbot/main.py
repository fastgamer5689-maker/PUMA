"""Entry point:  python main.py"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from database import Database
from utils.batch_logger import BatchLogger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("modbot")

EXTENSIONS = (
    "cogs.onboarding",
    "cogs.logging_cog",
    "cogs.automod",
    "cogs.antinuke",
    "cogs.ghostping",
    "cogs.quarantine",
    "cogs.tickets",
    "cogs.ai_chat",
)


class ModBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()   # includes guilds, bans/audit-log (moderation), messages
        intents.members = True                # PRIVILEGED: member joins, role changes, quarantine
        intents.message_content = True        # PRIVILEGED: slur filter, logging, AI chat
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self.db = Database(config.DB_PATH)
        self.batch = BatchLogger(self)

    async def setup_hook(self) -> None:
        await self.db.connect()
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        self.tree.on_error = self.on_tree_error
        await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Logged in as %s in %d guild(s)", self.user, len(self.guilds))

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        # Quietly ignore non-existent commands like typed text starting with prefixes
        if isinstance(error, commands.CommandNotFound):
            return
        raise error

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            msg = "You don't have permission to use that command."
        else:
            log.exception("Slash command error", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    async def close(self) -> None:
        await super().close()
        await self.db.close()


def main() -> None:
    if not config.DISCORD_TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in .env")
    ModBot().run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()