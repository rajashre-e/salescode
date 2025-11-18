# ------------------------------------------------------------
# myagent.py — FINAL NO-VAD VERSION (v1.3.2 stable)
# This version provides:
#  - Filler-word suppression (uh, hmm, haan)
#  - Hard interrupt detection (stop, wait, hold on)
#  - Manual LLM routing after bypassing LiveKit’s auto-pipeline
#  - TTS speaking grace window to avoid false interruptions
#  - Extremely defensive hook disabling to prevent transcript leaks
# ------------------------------------------------------------

from dotenv import load_dotenv
load_dotenv()

import os
import re
import unicodedata
import asyncio
import time
import logging
from typing import List

# LiveKit Agent & Session handling
from livekit.agents import Agent, AgentSession, JobContext
from livekit.agents import WorkerOptions, cli

# Deepgram speech & TTS models
from livekit.plugins.deepgram import TTS as DeepgramTTS
from livekit.plugins import deepgram


# ============================================================
# CONFIGURATION
# ============================================================

# Grace period (in ms) after TTS starts speaking during which
# the agent *should NOT* treat user sounds as an interruption
TTS_SPEAKING_GRACE_MS = 250

# Words that we want to completely ignore as fillers
DEFAULT_IGNORED = ["uh", "umm", "hmm", "mm", "mhm", "mmhmm", "haan"]

# Words that should ALWAYS interrupt TTS immediately
DEFAULT_INTERRUPTS = [
    "stop", "wait", "no", "hold",
    "holdon", "hold_on",
    "waitasecond", "wait_a_second",
    "waitasec", "one_second", "one_sec"
]


# ============================================================
# LOGGING SETUP
# ============================================================

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("interrupt-filter")
logger.setLevel(logging.DEBUG)

# More detailed logs for internal interrupt manager
logging.getLogger("interrupt-manager").setLevel(logging.DEBUG)

# Keep LiveKit logs visible for debugging
logging.getLogger("livekit.agents").setLevel(logging.DEBUG)


# ============================================================
# TOKENIZATION HELPERS
# ============================================================

def _normalize_text(s: str) -> str:
    """
    Normalize text:
      - Convert full-width → half-width characters
      - Strip accents/diacritics
      - Lowercase everything
    This helps with robust filler/interrupt detection.
    """
    s = unicodedata.normalize("NFKC", s or "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.lower()


def normalize_token(tok: str) -> str:
    """Normalize a single token, removing punctuation & symbols."""
    s = _normalize_text(tok)
    return re.sub(r"[^a-z0-9']+", "", s)


def tokenize(text: str) -> List[str]:
    """
    Extract tokens from text.
    Supports words like "don't" using a regex that keeps ' inside words.
    """
    if not text:
        return []
    raw = re.findall(r"[^\W_]+(?:'[^\W_]+)?", text, re.UNICODE)
    return [normalize_token(t) for t in raw if t.strip()]


# ============================================================
# FILLER DETECTION
# ============================================================

def is_simple_filler(tok: str) -> bool:
    """
    Detect if a token is a filler noise like:
      uh, hmm, haan, uhhhh, ummm, mmmm etc.

    This allows us to avoid false interruptions.
    """
    tok = (tok or "").lower()

    # direct matches
    if tok in {"uh", "umm", "hmm", "mm", "mhm", "mmhmm", "haan"}:
        return True

    # pattern-like fillers
    if re.fullmatch(r"u+h+", tok): return True
    if re.fullmatch(r"um+", tok): return True
    if re.fullmatch(r"h+m+", tok): return True
    if re.fullmatch(r"m+", tok): return True
    if re.fullmatch(r"mh+m+", tok): return True

    return False


async def extract_confidence(event) -> float:
    """
    Extract confidence score from the STT event.
    If Deepgram provides `event.confidence`, use it.
    Otherwise check inside `event.alternatives`.
    """
    if hasattr(event, "confidence") and event.confidence is not None:
        return float(event.confidence)

    alt = getattr(event, "alternatives", None)
    if alt and len(alt):
        c = getattr(alt[0], "confidence", None)
        if c is not None:
            return float(c)

    # If no confidence provided, assume 1.0 (safe default)
    return 1.0


# ============================================================
# INTERRUPT MANAGER
# ------------------------------------------------------------
# This class receives STT events and decides:
#   - ignore (useless noise)
#   - interrupt_block (hard stop like "stop")
#   - interrupt (real user speech interruption)
#   - accept (valid user message → forward to LLM)
#   - log_only (quiet filler, ignore)
# ============================================================

class InterruptManager:
    def __init__(self, session, ignored_words, interrupt_words, logger=None):
        """
        session: LiveKit AgentSession instance
        ignored_words: words to always ignore (fillers)
        interrupt_words: words that always break TTS
        """
        self.session = session

        # Normalize ignored/interrupt words for comparison
        self.ignored = set(self._norm(w) for w in (ignored_words or []))
        self.interrupt = set(self._norm(w) for w in (interrupt_words or []))

        self.logger = logger or logging.getLogger("interrupt-manager")

        self._lock = asyncio.Lock()

        # Track when TTS last began speaking
        self._last_tts_start_ms = 0
        self.tts_speaking_grace_ms = TTS_SPEAKING_GRACE_MS

    # -----------------------------
    # TEXT NORMALIZATION UTILITIES
    # -----------------------------
    @staticmethod
    def _norm(s: str) -> str:
        """Normalize for internal comparison."""
        return re.sub(r"[^a-z0-9']", "", _normalize_text(s or ""))

    # -----------------------------
    # TTS STATE HANDLING
    # -----------------------------
    async def mark_tts_start(self):
        """Record timestamp when TTS starts speaking."""
        async with self._lock:
            self._last_tts_start_ms = int(time.time() * 1000)

    async def agent_is_speaking(self):
        """
        Check if agent is currently in its grace period where speech
        should NOT be interrupted by short sounds.
        """
        async with self._lock:
            if self._last_tts_start_ms == 0:
                return False

            elapsed = int(time.time() * 1000) - self._last_tts_start_ms
            return elapsed <= self.tts_speaking_grace_ms

    # -----------------------------
    # MAIN DECISION LOGIC
    # -----------------------------
    async def handle_transcription(self, event):
        """
        Analyze the STT event and classify it as:
        ignore / interrupt_block / interrupt / accept / log_only
        """
        text = getattr(event, "text", "") or ""
        tokens = tokenize(text)
        conf = await extract_confidence(event)
        speaking = await self.agent_is_speaking()

        info = {"text": text, "tokens": tokens, "confidence": conf, "speaking": speaking}
        self.logger.debug("[STT EVENT] %s", info)

        # 1. No tokens = nothing meaningful
        if not tokens:
            self.logger.info("[IGNORED EMPTY] %s", info)
            return "ignore"

        # 2. Check for filler patterns
        all_fillers = all(is_simple_filler(t) for t in tokens)

        # 3. Check for interrupt keywords
        any_interrupt = any(self._norm(t) in self.interrupt for t in tokens)

        # -----------------------------------------------------
        # STEP 1: HIGH PRIORITY — INTERRUPT WORDS ALWAYS WIN
        # -----------------------------------------------------
        if any_interrupt:
            self.logger.info("[INTERRUPT WORD DETECTED - BLOCKING] speaking=%s | %s", speaking, info)

            # Stop TTS immediately
            try:
                await self.session.stop_tts()
            except Exception as e:
                self.logger.exception("Error stopping TTS on interrupt: %s", e)

            return "interrupt_block"

        # -----------------------------------------------------
        # STEP 2: IF AGENT IS SPEAKING
        # -----------------------------------------------------
        if speaking:

            # Filler during speaking? → ignore
            if all_fillers:
                self.logger.info("[IGNORED FILLER WHILE SPEAKING] %s", info)
                return "ignore"

            # Low confidence short noise? → ignore
            if len(tokens) <= 2 and conf < 0.75:
                self.logger.info("[IGNORED LOWCONF] conf=%.2f | %s", conf, info)
                return "ignore"

            # Real user speech → treat as interrupt
            self.logger.info("[INTERRUPT REAL SPEECH] %s", info)
            try:
                await self.session.stop_tts()
            except Exception as e:
                self.logger.exception("Error stopping TTS on real speech interrupt: %s", e)

            return "interrupt"

        # -----------------------------------------------------
        # STEP 3: AGENT IS QUIET
        # -----------------------------------------------------
        if all_fillers:
            # Quiet filler like "umm" — log but don't forward
            self.logger.info("[LOG_ONLY - FILLER QUIET] %s", info)
            return "log_only"

        # Normal valid speech → accept
        self.logger.info("[ACCEPT NORMAL] %s", info)
        return "accept"
