"""Effect engine — runs a chosen effect at fixed FPS, streams frames to bridge."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from effects import build_frame
from effects_lib import EFFECTS, Effect

log = logging.getLogger("effect-engine")

FRAME_RATE_HZ = 12
FRAME_PERIOD = 1.0 / FRAME_RATE_HZ


class EffectEngine:
    def __init__(self, bridge_url: str) -> None:
        self.bridge = httpx.AsyncClient(base_url=bridge_url, timeout=2.0)
        self._task: Optional[asyncio.Task] = None
        self._stop_flag = False
        self._effect: Optional[Effect] = None
        self._effect_name: Optional[str] = None
        self._t0: float = 0.0

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def current_effect(self) -> Optional[str]:
        return self._effect_name if self.active else None

    async def start(self, name: str) -> None:
        if name not in EFFECTS:
            raise ValueError(f"unknown effect: {name}")
        await self.stop(restore_mode=False)
        self._stop_flag = False
        self._effect = EFFECTS[name]()
        self._effect_name = name
        self._t0 = time.monotonic()
        self._task = asyncio.create_task(self._run())
        log.info("effect started: %s", name)

    async def stop(self, restore_mode: bool = True) -> None:
        if self._task and not self._task.done():
            self._stop_flag = True
            try:
                await asyncio.wait_for(self._task, timeout=1.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None
        self._effect = None
        self._effect_name = None
        if restore_mode:
            await self._send_line("mode:infinity")

    async def _send_line(self, line: str) -> None:
        try:
            await self.bridge.post("/command", json={"line": line}, timeout=0.4)
        except Exception as e:
            log.debug("bridge send failed: %s", e)

    async def _run(self) -> None:
        while not self._stop_flag:
            t_ms = int((time.monotonic() - self._t0) * 1000)
            try:
                panel_leds = self._effect.render(t_ms)
                await self._send_line("frame:" + build_frame(panel_leds))
            except Exception as e:
                log.warning("render/send error: %s", e)
            await asyncio.sleep(FRAME_PERIOD)
