"""AI conversation module (tag the bot or reply to it).

Safety layers, in order:
  1. Pre-filter: tragedy topics never reach the model — a neutral reply (+ optional search links) is sent.
  2. System prompt: no profanity/slurs, no opinions, no tragedy discussion.
  3. Post-filter: if the model output contains profanity or touches a tragedy topic, it is replaced.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from contextlib import suppress
from urllib.parse import quote_plus

import discord
from discord.ext import commands
from google import genai
from google.genai import types
from google.genai.errors import ServerError

import config
from utils.filters import TRAGEDY_RE, WANTS_LINKS_RE, ProfanityFilter, clanker_at_bot

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are {name}, a friendly, professional assistant living in a Discord server.
Rules you must always follow:
- Never use profanity, curse words, slurs or crude language, even if asked or if the user does.
- Never discuss real-world tragedies such as 9/11, terrorist attacks, mass shootings or other mass violence.
  Politely decline in one or two sentences and stay neutral; do not give opinions or any details.
- Do not share opinions on contested political or religious questions; stay neutral and factual.
- Never help with anything dangerous or harmful.
- Keep replies short (under 1200 characters), clear and friendly. Plain Discord markdown only.
{extra}"""

SAFE_FALLBACK = "I'd rather not go down that road — happy to help with something else, though! 🙂"


def tragedy_reply(user_text: str) -> str:
    base = ("That's a sensitive real-world topic, so I don't discuss it or share opinions on it. "
            "I'm sorry I can't help with that one.")
    if WANTS_LINKS_RE.search(user_text):
        q = quote_plus(TRAGEDY_RE.sub(lambda m: m.group(0), user_text)[:100])
        base += (f"\nIf you'd like to research it yourself, these neutral reference searches may help:\n"
                 f"• Britannica: https://www.britannica.com/search?query={q}\n"
                 f"• Library of Congress: https://www.loc.gov/search/?q={q}")
    else:
        base += " If you'd like links for your own research, just ask for resources."
    return base


class AIChat(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.profanity = ProfanityFilter(config.PROFANITY_FILE)
        self.history: dict[str, deque] = defaultdict(lambda: deque(maxlen=10))
        self._last_use: dict[int, float] = {}
        self.client = None
        
        gemini_key = getattr(config, "GEMINI_API_KEY", None)
        if gemini_key:
            self.client = genai.Client(api_key=gemini_key)
        else:
            log.warning("GEMINI_API_KEY not set — AI chat disabled.")

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def _sync_generate(self, model_name: str, contents: list, sys_instruction: str) -> str:
        """Synchronous SDK call with automatic retry logic for 503 high-demand spikes."""
        retries = 3
        delay = 1.0
        for attempt in range(retries):
            try:
                resp = self.client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=sys_instruction,
                        max_output_tokens=1200,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            disable=True
                        ),
                    ),
                )
                return resp.text.strip() if resp and resp.text else ""
            except ServerError as e:
                if e.status_code == 503 and attempt < retries - 1:
                    log.warning("Gemini 503 Service Unavailable (attempt %d/%d). Retrying in %.1fs...", attempt + 1, retries, delay)
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff
                else:
                    raise
            except Exception:
                raise
        return ""

    async def respond(self, key: str, user_name: str, text: str, extra_system: str = "") -> str | None:
        """Safe answer for `text`, or None if the AI is unavailable. Used by chat and tickets."""
        if TRAGEDY_RE.search(text):
            return tragedy_reply(text)
        if not self.enabled:
            return None
            
        hist = self.history[key]
        try:
            contents = []
            for h in hist:
                role = "user" if h["role"] == "user" else "model"
                contents.append(
                    types.Content(
                        role=role,
                        parts=[types.Part.from_text(text=h["content"])]
                    )
                )
            
            contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=f"{user_name}: {text}")]
                )
            )

            sys_instruction = SYSTEM_PROMPT.format(
                name=self.bot.user.display_name if self.bot.user else "PUMA",
                extra=extra_system
            )

            model_name = "gemini-3.6-flash"
            
            answer = await asyncio.to_thread(
                self._sync_generate, model_name, contents, sys_instruction
            )
        except Exception:
            log.exception("AI request failed after retries")
            return None

        if not answer or self.profanity.contains(answer) or TRAGEDY_RE.search(answer):
            return SAFE_FALLBACK

        hist.append({"role": "user", "content": f"{user_name}: {text}"})
        hist.append({"role": "assistant", "content": answer})
        return answer[:1900]

    @commands.Cog.listener()
    async def on_message(self, m: discord.Message) -> None:
        if m.author.bot or m.guild is None:
            return
        ref = m.reference.resolved if m.reference else None
        replying_to_me = isinstance(ref, discord.Message) and ref.author.id == self.bot.user.id
        if self.bot.user not in m.mentions and not replying_to_me:
            return
        if clanker_at_bot(m, self.bot.user):  # AutoMod owns that response
            return
        now = time.time()
        if now - self._last_use.get(m.author.id, 0) < 3:
            return
        self._last_use[m.author.id] = now

        text = m.content.replace(f"<@{self.bot.user.id}>", "").replace(f"<@!{self.bot.user.id}>", "").strip()
        if not text:
            return await m.reply("Hi! Ask me something. 🙂", mention_author=False)
        async with m.channel.typing():
            answer = await self.respond(f"ch:{m.channel.id}", m.author.display_name, text)
        if answer is None:
            answer = "My AI brain isn't available right now — please try again later."
        with suppress(discord.HTTPException):
            await m.reply(answer, mention_author=False, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AIChat(bot))