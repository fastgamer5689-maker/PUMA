"""Content filters: slur matching, profanity, tragedy topics, clanker detection, NSFW images.

Slur term lists are NOT hard-coded: put terms (one per line) in filters/*.txt.
Matching is done on normalized text (leetspeak, accents, zero-width chars, repeated
letters, separators like "n.i.g..."), anchored on letter boundaries so innocent words
that merely *contain* a term are not matched.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import tempfile
import unicodedata
from pathlib import Path

import discord

log = logging.getLogger(__name__)

_LEET = str.maketrans({"@": "a", "4": "a", "3": "e", "1": "i", "!": "i", "|": "i",
                       "0": "o", "$": "s", "5": "s", "7": "t", "+": "t"})
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.translate(_ZERO_WIDTH).lower().translate(_LEET)


def _term_pattern(term: str) -> str:
    letters = [re.escape(c) for c in normalize(term) if c.isalnum()]
    if not letters:
        return ""
    body = r"[\W_]*".join(f"{l}+" for l in letters)       # tolerate repeats + separators
    return rf"(?<![a-z0-9]){body}(?:s|z|es)?(?![a-z0-9])"  # letter-boundary anchored, plural tolerant


def _load_terms(path: Path) -> list[str]:
    if not path.exists():
        log.warning("Filter file missing: %s", path)
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def _compile(terms: list[str]) -> re.Pattern | None:
    pats = [p for p in (_term_pattern(t) for t in terms) if p]
    return re.compile("|".join(f"(?:{p})" for p in pats)) if pats else None


class SlurFilter:
    """Returns 'hard' (instant-ban list), 'soft' (flag + human review) or None."""

    def __init__(self, hard_file: Path, soft_file: Path):
        self._hard = _compile(_load_terms(hard_file))
        self._soft = _compile(_load_terms(soft_file))

    def check(self, text: str) -> str | None:
        if not text:
            return None
        norm = normalize(text)
        if self._hard and self._hard.search(norm):
            return "hard"
        if self._soft and self._soft.search(norm):
            return "soft"
        return None


class ProfanityFilter:
    """Used on the BOT's own output so it can never swear."""

    def __init__(self, path: Path):
        self._rx = _compile(_load_terms(path))

    def contains(self, text: str) -> bool:
        return bool(self._rx and self._rx.search(normalize(text)))


# --------------------------------------------------------------------------
# Tragedy topics (editable) — the AI module will not discuss these.
# --------------------------------------------------------------------------
TRAGEDY_RE = re.compile(
    r"\b(9\s*/\s*11|9-11|sept(ember)?\s*11(th)?|twin\s+towers|terror(ist|ism)?\s+attacks?|"
    r"mass\s+(shooting|murder|killing)s?|school\s+shootings?|massacres?|genocide|"
    r"suicide\s+bomb\w*|bombings?)\b",
    re.I,
)
WANTS_LINKS_RE = re.compile(
    r"\b(links?|resources?|sources?|read\s+more|learn\s+more|where\s+(can|do)\s+i\s+(read|learn|find))\b", re.I
)

# --------------------------------------------------------------------------
# "Clanker" — NOT bannable. Only intercepted when aimed at the bot.
# --------------------------------------------------------------------------
CLANKER_RE = re.compile(r"(?<![a-z])cl[a4]nk+[e3]r[sz]?(?![a-z])", re.I)


def clanker_at_bot(message: discord.Message, bot_user: discord.ClientUser) -> bool:
    if not message.content or not CLANKER_RE.search(message.content):
        return False
    if bot_user in message.mentions:
        return True
    ref = message.reference
    if ref and isinstance(ref.resolved, discord.Message) and ref.resolved.author.id == bot_user.id:
        return True
    lowered = message.content.lower()
    return bot_user.name.lower() in lowered


# --------------------------------------------------------------------------
# NSFW image classification (NudeNet). Optional dependency.
# --------------------------------------------------------------------------
class NSFWClassifier:
    def __init__(self, thresholds: dict[str, float]):
        self.thresholds = thresholds
        self._detector = None
        try:
            from nudenet import NudeDetector  # type: ignore
            self._detector = NudeDetector()
        except Exception as exc:  # pragma: no cover - optional dependency
            log.warning("NSFW image scanning disabled (%s). pip install nudenet pillow", exc)

    @property
    def enabled(self) -> bool:
        return self._detector is not None

    def _scan(self, data: bytes) -> bool:
        from PIL import Image  # type: ignore
        img = Image.open(io.BytesIO(data)).convert("RGB")  # first frame for GIF/WebP
        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            img.save(path, "JPEG")
            for det in self._detector.detect(path):
                if det["score"] >= self.thresholds.get(det["class"], 2.0):
                    return True
            return False
        finally:
            os.remove(path)

    async def is_explicit(self, data: bytes) -> bool:
        if not self.enabled:
            return False
        try:
            return await asyncio.to_thread(self._scan, data)
        except Exception:
            log.exception("NSFW scan failed")
            return False
