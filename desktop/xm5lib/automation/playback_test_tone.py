"""Bounded generated tone through the already connected playback Session.

Engine's playback intent lock/idempotency receipt encloses this synchronous
operation. No output selection, library registration, process or network input.
"""
from __future__ import annotations

import math
from pathlib import Path
import struct
import tempfile
import time
import wave

from ..application import DomainError
from .runtime import no_links, secure_directory


def test_tone(engine, arguments=None):
    args = arguments or {}
    if set(args) - {"duration_seconds", "frequency_hz"}:
        raise DomainError("INVALID_ARGUMENT", "Only bounded tone duration/frequency are accepted")
    duration, frequency = args.get("duration_seconds", 1.2), args.get("frequency_hz", 660)
    if (type(duration) not in (int, float) or not math.isfinite(duration) or not 1 <= duration <= 5 or
        type(frequency) not in (int, float) or not math.isfinite(frequency) or not 220 <= frequency <= 2000):
        raise DomainError("INVALID_ARGUMENT", "Tone duration 1..5 seconds; frequency 220..2000 Hz")
    session = engine.playback
    if session is None:
        raise DomainError("PLAYBACK_UNAVAILABLE", "Choose and connect an explicit output first")
    before = session.status()
    if before.get("session_closed") or not before.get("synchronized", True):
        raise DomainError("PLAYBACK_OUTCOME_UNKNOWN", "Reconcile playback before a test tone")
    runtime = Path(engine.home) / "runtime" / "test-tone"
    no_links(runtime)
    secure_directory(runtime)
    original_queue = list(before.get("queue") or [])
    original_index = before.get("index") or 0
    original_position = max(0, int(before.get("position_ms") or 0))
    original_transport = before.get("transport")

    def wait_format(timeout=3):
        deadline = time.monotonic() + timeout
        while True:
            state = session.status()
            if state.get("session_closed") or state.get("bridge_error"):
                raise DomainError("PLAYBACK_FAILED", "Playback failed during test tone", state)
            if not state.get("awaiting_format"):
                return state
            if time.monotonic() >= deadline:
                raise DomainError("PLAYBACK_OUTCOME_UNKNOWN", "Decoder format acknowledgement timed out", state)
            time.sleep(.01)

    # -36 dBFS amplitude, short fade-in/out, user's volume/output/PEQ untouched.
    with tempfile.TemporaryDirectory(prefix="tone-", dir=runtime) as directory:
        path = Path(directory) / "tone.wav"
        rate, frames = 48000, int(duration * 48000)
        with wave.open(str(path), "wb") as output:
            output.setparams((1, 2, rate, frames, "NONE", "not compressed"))
            output.writeframes(b"".join(struct.pack("<h", round(32767 * 10 ** (-36 / 20) *
                min(1, i / 480, (frames - 1 - i) / 480) * math.sin(2 * math.pi * frequency * i / rate))) for i in range(frames)))
        failure = None
        try:
            session.play_tracks([str(path)], source_kinds={str(path): "pcm"})
            wait_format()
            deadline = time.monotonic() + duration + .25
            while time.monotonic() < deadline:
                state = session.status()
                if state.get("session_closed") or state.get("bridge_error"):
                    raise DomainError("PLAYBACK_FAILED", "Test tone did not complete", state)
                if state.get("transport") == "stopped":
                    break
                time.sleep(.01)
        except Exception as exc:
            failure = exc
        finally:
            session.command("stop")
            if original_queue:
                session.play_tracks(original_queue, original_index)
                wait_format()
                if original_position:
                    session.command("seek", {"position_ms": original_position})
                if original_transport != "playing" and not before.get("play_requested"):
                    session.command("pause" if original_transport == "paused" else "stop")
            else:
                session.command("queue.replace", {"paths": []})
        if failure is not None:
            raise failure
    after = session.status()
    return {"tone_completed": True, "duration_seconds": duration, "frequency_hz": frequency,
            "amplitude_dbfs": -36, "queue_restored": after.get("queue") == original_queue, "playback": after}
