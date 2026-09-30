"""Text-to-speech on a background thread, using the OS voice (SAPI5 on Windows)."""

from __future__ import annotations

import logging
import queue
import threading

log = logging.getLogger(__name__)


class Voice:
    def __init__(self, rate: int = 185):
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._rate = rate
        self._thread = threading.Thread(target=self._run, name="voice", daemon=True)
        self._thread.start()

    def say(self, text: str) -> None:
        # Only the newest tip matters; drop anything still waiting.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._queue.put(text)

    def close(self) -> None:
        self._queue.put(None)

    def _run(self) -> None:
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", self._rate)
        except Exception as e:  # no TTS backend available
            log.warning("Voice disabled: %s", e)
            return
        while (text := self._queue.get()) is not None:
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception as e:
                log.warning("Couldn't speak tip: %s", e)
