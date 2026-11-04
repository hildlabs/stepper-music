"""The five effects."""
from __future__ import annotations

import math

from effects import (
    Effect, PALETTE, PANEL_LEDS, NUM_PANELS, RGB,
    clamp, lerp, lerp_rgb, scale_rgb, ccw_index, ring_distance,
)


# ── 1. Viral Idle Glow ───────────────────────────────────────────────────
class IdleGlow(Effect):
    """Slow per-voice breathing in the main colour. High-end TikTok ambient."""
    name = "idle_glow"
    label = "Idle Glow"

    # per-voice pulse periods (ms) and depths (0..1)
    PERIOD_MS = (4200, 6800, 5400, 8200)   # melody fastest, harm-dn slowest
    DEPTH     = (0.45,  0.30, 0.30, 0.18)   # melody breathes most, harm-dn least
    BASELINE  = (0.55,  0.45, 0.45, 0.40)
    HUE_DRIFT_VOICE3 = 0.18                  # subtle main↔accent shift on harm↑

    def render(self, t_ms: int) -> list[list[RGB]]:
        frame: list[list[RGB]] = []
        for i, voice in enumerate(PALETTE):
            phase = (t_ms % self.PERIOD_MS[i]) / self.PERIOD_MS[i]
            # smooth (1 - cos) curve, not a sawtooth
            wave = 0.5 - 0.5 * math.cos(phase * 2 * math.pi)
            brightness = self.BASELINE[i] + wave * self.DEPTH[i]
            brightness *= voice.base_brightness

            # harmony↑ slowly drifts cyan ↔ mint
            if i == 2:
                drift_t = 0.5 + 0.5 * math.sin(phase * 2 * math.pi)
                color = lerp_rgb(voice.main, voice.accent, drift_t * self.HUE_DRIFT_VOICE3 * 5.0)
            else:
                color = voice.main

            led = scale_rgb(color, brightness)
            frame.append([led] * PANEL_LEDS)
        return frame


# ── 2. Counter-Clockwise Gear Chase ──────────────────────────────────────
class GearChase(Effect):
    """A bright dot per panel runs CCW with a soft trail."""
    name = "gear_chase"
    label = "Gear Chase"

    PERIOD_MS = (1800, 2600, 2200, 3000)   # melody fastest
    TRAIL_LEN = 5                           # LEDs in the trail
    BASE_LEVEL = 0.10                       # background glow

    def render(self, t_ms: int) -> list[list[RGB]]:
        frame: list[list[RGB]] = []
        for i, voice in enumerate(PALETTE):
            phase = (t_ms % self.PERIOD_MS[i]) / self.PERIOD_MS[i]  # 0..1
            head_step = phase * PANEL_LEDS
            head_idx = ccw_index(int(head_step))
            head_frac = head_step - int(head_step)

            panel: list[RGB] = []
            for led in range(PANEL_LEDS):
                # background base
                bg = scale_rgb(voice.main, self.BASE_LEVEL * voice.base_brightness)

                # find ring distance walking CCW (negative direction)
                # dist measured along the chase direction:
                ccw_step_for_led = (-led) % PANEL_LEDS
                dist = (ccw_step_for_led - int(head_step)) % PANEL_LEDS

                if dist == 0:
                    # head, brighter side toward the tail
                    intensity = 1.0 - 0.4 * head_frac
                    color = scale_rgb(voice.main, intensity * voice.base_brightness)
                    panel.append((
                        max(bg[0], color[0]),
                        max(bg[1], color[1]),
                        max(bg[2], color[2]),
                    ))
                elif dist <= self.TRAIL_LEN:
                    # exponential trail fade in the accent colour
                    fade = math.pow(0.55, dist - 1) * (1.0 - 0.4 * head_frac if dist == 1 else 1.0)
                    color = scale_rgb(voice.accent, fade * voice.base_brightness)
                    panel.append((
                        max(bg[0], color[0]),
                        max(bg[1], color[1]),
                        max(bg[2], color[2]),
                    ))
                else:
                    panel.append(bg)
            frame.append(panel)
        return frame


# ── 3. Beat Pulse (simulated tempo) ──────────────────────────────────────
class BeatPulse(Effect):
    """Each panel pulses to its own simulated tempo & character."""
    name = "beat_pulse"
    label = "Beat Pulse"

    BPM = 100                                # global tempo
    BEAT_MS = 60000 // BPM                   # 600ms @ 100 BPM
    # melody: every beat sharp accent
    # bass:   every 2 beats deep slow pulse
    # harm↑:  every beat upward sweep
    # harm↓:  every 2 beats downward sweep

    def _accent_envelope(self, t_in_beat: float, attack: float = 0.04, decay: float = 0.6) -> float:
        """Sharp attack, exponential decay. t_in_beat in [0,1)."""
        if t_in_beat < attack:
            return t_in_beat / attack
        return math.exp(-(t_in_beat - attack) / decay)

    def render(self, t_ms: int) -> list[list[RGB]]:
        frame: list[list[RGB]] = []

        # voice 1: melody — bright accents on every beat
        v = PALETTE[0]
        beat_phase = (t_ms % self.BEAT_MS) / self.BEAT_MS
        env = self._accent_envelope(beat_phase, 0.03, 0.45)
        intensity = 0.20 + 0.80 * env
        led_color = lerp_rgb(v.main, v.accent, env * 0.3)
        frame.append([scale_rgb(led_color, intensity * v.base_brightness)] * PANEL_LEDS)

        # voice 2: bass — slow deep pulse on every 2nd beat
        v = PALETTE[1]
        slow_phase = (t_ms % (self.BEAT_MS * 2)) / (self.BEAT_MS * 2)
        env = self._accent_envelope(slow_phase, 0.08, 0.9)
        intensity = 0.30 + 0.70 * env
        led_color = lerp_rgb(v.accent, v.main, env)
        frame.append([scale_rgb(led_color, intensity * v.base_brightness)] * PANEL_LEDS)

        # voice 3: harmony↑ — soft upward sweep, bottom→top, every beat
        v = PALETTE[2]
        sweep_phase = (t_ms % self.BEAT_MS) / self.BEAT_MS  # 0..1
        # vertical position in normalized 0..1 (0=bottom, 1=top)
        head_y = sweep_phase
        panel: list[RGB] = []
        for led in range(PANEL_LEDS):
            y = self._led_vertical_position(led)
            d = abs(y - head_y)
            sweep_brightness = math.exp(-d * 8.0)
            base = 0.20
            intensity = base + sweep_brightness * 0.8
            color = lerp_rgb(v.main, v.accent, sweep_brightness)
            panel.append(scale_rgb(color, intensity * v.base_brightness))
        frame.append(panel)

        # voice 4: harmony↓ — soft downward sweep, every 2nd beat
        v = PALETTE[3]
        slow_phase = (t_ms % (self.BEAT_MS * 2)) / (self.BEAT_MS * 2)
        head_y = 1.0 - slow_phase
        panel = []
        for led in range(PANEL_LEDS):
            y = self._led_vertical_position(led)
            d = abs(y - head_y)
            sweep_brightness = math.exp(-d * 6.0)
            base = 0.18
            intensity = base + sweep_brightness * 0.7
            color = lerp_rgb(v.main, v.accent, sweep_brightness * 0.6)
            panel.append(scale_rgb(color, intensity * v.base_brightness))
        frame.append(panel)

        return frame

    @staticmethod
    def _led_vertical_position(led: int) -> float:
        """Return 0 (bottom) .. 1 (top) for a given LED index on the ring."""
        # 0..3   bottom edge → y=0
        # 4..7   right edge bottom→top → y=0..1
        # 8..11  top edge → y=1
        # 12..15 left edge top→bottom → y=1..0
        if led <= 3:
            return 0.0
        if led <= 7:
            return (led - 3) / 4.0          # 4→0.25, 7→1.0
        if led <= 11:
            return 1.0
        return 1.0 - ((led - 11) / 4.0)     # 12→0.75, 15→0.0


# ── 4. Build Up ──────────────────────────────────────────────────────────
class BuildUp(Effect):
    """Panel-by-panel intro 4→3→2→1, then handoff to idle glow.

    Total intro is ~2.4s so the TikTok hook lands inside the first 3s.
    """
    name = "build_up"
    label = "Build Up"

    # When each panel enters (ms) and how long its ramp-up lasts
    ENTRIES = ((1800, 600),   # voice 1 melody — last (the climax)
               (1200, 600),   # voice 2 bass
               ( 600, 600),   # voice 3 harm↑
               (   0, 600))   # voice 4 harm↓ — first
    HANDOFF_MS = 2600          # after this, we're in idle-glow steady state

    def __init__(self) -> None:
        self._idle = IdleGlow()

    def render(self, t_ms: int) -> list[list[RGB]]:
        if t_ms >= self.HANDOFF_MS:
            return self._idle.render(t_ms)

        frame: list[list[RGB]] = []
        for i, voice in enumerate(PALETTE):
            entry_ms, ramp_ms = self.ENTRIES[i]
            if t_ms < entry_ms:
                # not yet — very dim
                frame.append([scale_rgb(voice.main, 0.02 * voice.base_brightness)] * PANEL_LEDS)
                continue
            ramp_t = clamp((t_ms - entry_ms) / ramp_ms)
            # ease-out cubic
            ramp_t = 1 - math.pow(1 - ramp_t, 3)
            intensity = 0.05 + ramp_t * voice.base_brightness
            # subtle accent flash at the moment of entry
            accent_flash = math.exp(-(t_ms - entry_ms) / 220.0) * 0.4
            color = lerp_rgb(voice.main, voice.accent, accent_flash)
            frame.append([scale_rgb(color, intensity)] * PANEL_LEDS)
        return frame


# ── 5. Loop Reset Flash ──────────────────────────────────────────────────
class LoopResetFlash(Effect):
    """Short coloured re-bloom on every panel for clip looping.

    Cycle: flash (0..220ms), settle (220..900ms), idle (900ms..LOOP_MS).
    LOOP_MS controls the overall clip length the flash pretends to mark.
    """
    name = "loop_reset"
    label = "Loop Reset"
    LOOP_MS = 4000

    def __init__(self) -> None:
        self._idle = IdleGlow()

    def render(self, t_ms: int) -> list[list[RGB]]:
        cycle_t = t_ms % self.LOOP_MS
        if cycle_t < 900:
            # bloom envelope: rise fast, decay slower
            if cycle_t < 220:
                env = cycle_t / 220.0
            else:
                env = math.exp(-(cycle_t - 220) / 280.0)
            frame: list[list[RGB]] = []
            for voice in PALETTE:
                # bloom uses accent at peak, falls back to main
                color = lerp_rgb(voice.main, voice.accent, env * 0.7)
                intensity = (0.35 + env * 0.85) * voice.base_brightness
                frame.append([scale_rgb(color, intensity)] * PANEL_LEDS)
            return frame
        # rest of the cycle: idle glow with adjusted phase so it feels seamless
        return self._idle.render(t_ms)


EFFECTS: dict[str, type[Effect]] = {
    e.name: e for e in (IdleGlow, GearChase, BeatPulse, BuildUp, LoopResetFlash)
}


def list_effects() -> list[dict]:
    return [{"name": cls.name, "label": cls.label} for cls in EFFECTS.values()]
