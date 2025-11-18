# myagent.py — FINAL NO-VAD VERSION (v1.3.2 safe: Filler-ignore + HARD INTERRUPT BLOCK + Manual LLM routing)
from dotenv import load_dotenv
load_dotenv()

import os
import re
import unicodedata
import asyncio
import time
import logging
from typing import List

from livekit.agents import Agent, AgentSession, JobContext
from livekit.agents import WorkerOptions, cli

# Deepgram TTS + STT
from livekit.plugins.deepgram import TTS as DeepgramTTS
from livekit.plugins import deepgram

# ---------------------------
# CONFIG
# ---------------------------
TTS_SPEAKING_GRACE_MS = 250

DEFAULT_IGNORED = ["uh", "umm", "hmm", "mm", "mhm", "mmhmm", "haan"]
DEFAULT_INTERRUPTS = [
    "stop", "wait", "no", "hold",
    "holdon", "hold_on",
    "waitasecond", "wait_a_second",
    "waitasec", "one_second", "one_sec"
]

# ---------------------------
# LOGGING
# ---------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("interrupt-filter")
logger.setLevel(logging.DEBUG)
logging.getLogger("interrupt-manager").setLevel(logging.DEBUG)
# keep LiveKit logs visible for debugging but we'll explicitly disable auto-routing below
logging.getLogger("livekit.agents").setLevel(logging.DEBUG)

# ---------------------------
# TOKEN HELPERS
# ---------------------------
def _normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.lower()

def normalize_token(tok: str) -> str:
    s = _normalize_text(tok)
    return re.sub(r"[^a-z0-9']+", "", s)

def tokenize(text: str) -> List[str]:
    if not text:
        return []
    raw = re.findall(r"[^\W_]+(?:'[^\W_]+)?", text, re.UNICODE)
    return [normalize_token(t) for t in raw if t.strip()]

# ---------------------------
# FILLER DETECTOR
# ---------------------------
def is_simple_filler(tok: str) -> bool:
    tok = (tok or "").lower()
    if tok in {"uh", "umm", "hmm", "mm", "mhm", "mmhmm", "haan"}:
        return True
    if re.fullmatch(r"u+h+", tok): return True
    if re.fullmatch(r"um+", tok): return True
    if re.fullmatch(r"h+m+", tok): return True
    if re.fullmatch(r"m+", tok): return True
    if re.fullmatch(r"mh+m+", tok): return True
    return False

async def extract_confidence(event) -> float:
    if hasattr(event, "confidence") and event.confidence is not None:
        return float(event.confidence)
    alt = getattr(event, "alternatives", None)
    if alt and len(alt):
        c = getattr(alt[0], "confidence", None)
        if c is not None:
            return float(c)
    return 1.0

# ---------------------------
# INTERRUPT MANAGER
# ---------------------------
class InterruptManager:
    def __init__(self, session, ignored_words, interrupt_words, logger=None):
        self.session = session
        self.ignored = set(self._norm(w) for w in (ignored_words or []))
        self.interrupt = set(self._norm(w) for w in (interrupt_words or []))
        self.logger = logger or logging.getLogger("interrupt-manager")

        self._lock = asyncio.Lock()
        self._last_tts_start_ms = 0
        self.tts_speaking_grace_ms = TTS_SPEAKING_GRACE_MS

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9']", "", _normalize_text(s or ""))

    async def mark_tts_start(self):
        async with self._lock:
            self._last_tts_start_ms = int(time.time() * 1000)

    async def agent_is_speaking(self):
        async with self._lock:
            if self._last_tts_start_ms == 0:
                return False
            return (int(time.time() * 1000) - self._last_tts_start_ms) <= self.tts_speaking_grace_ms

    # MAIN LOGIC
    async def handle_transcription(self, event):
        text = getattr(event, "text", "") or ""
        tokens = tokenize(text)
        conf = await extract_confidence(event)
        speaking = await self.agent_is_speaking()

        info = {"text": text, "tokens": tokens, "confidence": conf, "speaking": speaking}
        self.logger.debug("[STT EVENT] %s", info)

        if not tokens:
            self.logger.info("[IGNORED EMPTY] %s", info)
            return "ignore"

        all_fillers = all(is_simple_filler(t) for t in tokens)
        any_interrupt = any(self._norm(t) in self.interrupt for t in tokens)

        # ✅ CHECK FOR INTERRUPT WORDS FIRST - ALWAYS BLOCK THEM
        if any_interrupt:
            self.logger.info("[INTERRUPT WORD DETECTED - BLOCKING] speaking=%s | %s", speaking, info)
            # Stop TTS if speaking
            try:
                await self.session.stop_tts()
            except Exception as e:
                self.logger.exception("Error stopping TTS on interrupt: %s", e)
            return "interrupt_block"

        # AGENT SPEAKING
        if speaking:
            # filler while agent speaking -> ignore
            if all_fillers:
                self.logger.info("[IGNORED FILLER WHILE SPEAKING] %s", info)
                return "ignore"

            # low confidence noise while speaking
            if len(tokens) <= 2 and conf < 0.75:
                self.logger.info("[IGNORED LOWCONF] conf=%.2f | %s", conf, info)
                return "ignore"

            # otherwise treat as real interrupt and stop TTS
            self.logger.info("[INTERRUPT REAL SPEECH] %s", info)
            try:
                await self.session.stop_tts()
            except Exception as e:
                self.logger.exception("Error stopping TTS on real speech interrupt: %s", e)
            return "interrupt"

        # AGENT QUIET
        # filler when quiet -> log only, suppress forwarding
        if all_fillers:
            self.logger.info("[LOG_ONLY - FILLER QUIET] %s", info)
            return "log_only"

        # normal speech while quiet -> accept and forward
        self.logger.info("[ACCEPT NORMAL] %s", info)
        return "accept"

# ---------------------------
# ENTRYPOINT
# ---------------------------
async def entrypoint(ctx: JobContext):
    print("⚡ Connecting JobContext...")
    await ctx.connect()

    print("🎧 Room:", ctx.room)
    print("🚫 VAD DISABLED (continuous mode)")

    # STT
    stt = deepgram.STT(model="nova-2", interim_results=True)
    print("🧠 STT READY")

    agent = Agent(instructions="You are a real-time assistant.")

    session = AgentSession(
        stt=stt,
        llm="openai/gpt-4.1-mini",
        tts=DeepgramTTS(
            model="aura-asteria-en",
            encoding="linear16",
            sample_rate=16000,
        ),
    )

    # ---------------------------
    # CRITICAL: DISABLE LIVEKIT AUTO-STT → LLM ROUTING
    # (defensive: disable all known internal callback names / router / pipeline)
    # Must be done BEFORE session.start()
    # ---------------------------
    
    # Create a gate to control what goes to LLM
    blocked_texts = set()
    
    def create_blocking_wrapper(original_func):
        """Wrapper that checks our blocklist before calling original"""
        async def wrapper(*args, **kwargs):
            # Try to extract text from args
            text = None
            if args:
                first_arg = args[0]
                if isinstance(first_arg, str):
                    text = first_arg
                elif hasattr(first_arg, 'text'):
                    text = first_arg.text
            
            if text and text in blocked_texts:
                logger.warning(f"🚫 BLOCKED transcript from reaching LLM via {original_func.__name__}: {text}")
                return None
            
            if original_func:
                return await original_func(*args, **kwargs)
        return wrapper
    
    disabled = []
    POSSIBLE_CALLBACKS = [
        "_on_stt",
        "_on_user_transcript",
        "_on_transcription",
        "_handle_transcription",
        "_handle_user_transcript",
        "handle_user_transcript",
        "on_user_transcript",
        "receive_transcription",
        "add_user_message",
        "send_user_message",
        "ingest_user_message",
    ]
    
    for attr in POSSIBLE_CALLBACKS:
        if hasattr(session, attr):
            try:
                original = getattr(session, attr)
                if original and callable(original):
                    setattr(session, attr, create_blocking_wrapper(original))
                    disabled.append(f"{attr}(wrapped)")
                else:
                    setattr(session, attr, None)
                    disabled.append(f"{attr}(nulled)")
            except Exception:
                logger.exception("Could not wrap/unset %s", attr)

    # some builds route through session._router or session._pipeline
    if hasattr(session, "_router"):
        router = getattr(session, "_router")
        for attr in ("handle_user_transcript", "_handle_user_transcript", "handle_transcription"):
            if hasattr(router, attr):
                try:
                    original = getattr(router, attr)
                    if original and callable(original):
                        setattr(router, attr, create_blocking_wrapper(original))
                        disabled.append(f"_router.{attr}(wrapped)")
                    else:
                        setattr(router, attr, None)
                        disabled.append(f"_router.{attr}(nulled)")
                except Exception:
                    logger.exception("Could not wrap/unset _router.%s", attr)

    if hasattr(session, "_pipeline"):
        pipe = getattr(session, "_pipeline")
        for attr in ("handle_user_transcript", "_handle_user_transcript", "handle_transcription"):
            if hasattr(pipe, attr):
                try:
                    original = getattr(pipe, attr)
                    if original and callable(original):
                        setattr(pipe, attr, create_blocking_wrapper(original))
                        disabled.append(f"_pipeline.{attr}(wrapped)")
                    else:
                        setattr(pipe, attr, None)
                        disabled.append(f"_pipeline.{attr}(nulled)")
                except Exception:
                    logger.exception("Could not unset _pipeline.%s", attr)

    # Print what we disabled - helps debugging when testing live
    if disabled:
        print("🔥 Disabled/Wrapped auto-routing hooks:", disabled)
        logger.info("Disabled/Wrapped auto-routing hooks: %s", disabled)
    else:
        print("⚠️ No internal auto-routing hooks were found to disable. If transcripts still forward, report session internals.")
        logger.warning("No internal auto-routing hooks found/disabled on session.")

    # Create manager after disabling
    manager = InterruptManager(
        session=session,
        ignored_words=DEFAULT_IGNORED,
        interrupt_words=DEFAULT_INTERRUPTS,
    )

    # ---------------------------
    # track TTS start
    # ---------------------------
    async def tracked_say(text: str):
        await manager.mark_tts_start()
        logger.info("🔊 TTS: %s", text)
        try:
            await session.say(text)
        except Exception as e:
            logger.exception("session.say failed: %s", e)

    # ---------------------------
    # Manual forwarding helper (only path to LLM now)
    # ---------------------------
    async def forward_transcript_to_llm(event):
        text = getattr(event, "text", "") or ""
        # Try a few methods used in different session implementations
        try:
            if hasattr(session, "receive_transcription"):
                logger.debug("Forwarding via session.receive_transcription()")
                await session.receive_transcription(event)
                return True
            if hasattr(session, "add_user_message"):
                logger.debug("Forwarding via session.add_user_message(text)")
                await session.add_user_message(text)
                return True
            if hasattr(session, "send_user_message"):
                logger.debug("Forwarding via session.send_user_message(text)")
                await session.send_user_message(text)
                return True
            if hasattr(session, "ingest_user_message"):
                logger.debug("Forwarding via session.ingest_user_message(text)")
                await session.ingest_user_message(text)
                return True
            if hasattr(session, "_put_user_input"):
                logger.debug("Forwarding via session._put_user_input(text)")
                await session._put_user_input(text)
                return True
        except Exception as e:
            logger.exception("Error while forwarding transcript to LLM: %s", e)
            return False

        logger.warning("No known forward method found on session. Transcript NOT forwarded.")
        return False

    # ---------------------------
    # STT hook (centralized handling)
    # ---------------------------
    async def handle_stt(event):
        try:
            text = getattr(event, "text", "") or ""
            action = await manager.handle_transcription(event)

            logger.info(f"🔍 [HANDLER] text='{text}' action={action}")

            if action == "ignore":
                logger.debug("[HANDLER] action=ignore - no forwarding.")
                return

            if action == "log_only":
                logger.debug("[HANDLER] action=log_only - logged and suppressed forwarding.")
                return

            if action == "interrupt_block":
                logger.info(f"[HANDLER] ❌ interrupt_block - BLOCKED '{text}' from LLM")
                # Add to blocklist to prevent any other path from forwarding
                blocked_texts.add(text)
                # TTS was already stopped inside manager
                return

            if action == "interrupt":
                logger.info(f"[HANDLER] ⚠️ interrupt - TTS stopped for '{text}', NOT forwarding to LLM")
                # Add to blocklist
                blocked_texts.add(text)
                # TTS was already stopped inside manager
                return

            if action == "accept":
                logger.info(f"[HANDLER] ✅ accept - forwarding '{text}' to LLM")
                forwarded = await forward_transcript_to_llm(event)
                logger.info("[HANDLER] action=accept - forwarded=%s", forwarded)
                return

            logger.warning("[HANDLER] unknown action=%s - not forwarding", action)

        except Exception as e:
            logger.exception("Exception in handle_stt: %s", e)

    # listen to user_transcription events (your handler)
    session.on("user_transcription", lambda e: asyncio.create_task(handle_stt(e)))

    # start session (after we set up everything)
    print("🚀 Starting session...")
    await session.start(agent=agent, room=ctx.room)

    # initial greeting
    await tracked_say("Hello! You can talk to me anytime.")

# ---------------------------
# RUN
# ---------------------------
if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))