"""LED sync engine.

Streams a 4-panel × 16-LED frame to the LED bridge in time with the four
stepper voices. Each panel maps 1:1 to a voice; the colour palette is
the single source of truth in effects.py PALETTE — keep both surfaces
(standalone effects and song-sync) visually identical.

Per panel there are three layered effects:

  1. Base glow: idle (~10%) when voice is silent, full (~95%) when active,
     exponential decay between states.
  2. Wave: a bright accent LED rotating around the panel ring; its angular
     speed scales with the current note frequency, so high notes spin fast,
     low notes slowly. Visually conveys pitch without changing hue.
  3. Onset punch: at every note-start the panel briefly spikes brightness,
     gives a percussive "beat" feel even on sustained notes.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import math
import httpx

from effects import (
    PALETTE, NUM_PANELS, PANEL_LEDS,
    scale_rgb, rgb_to_hex, add_rgb, lerp_rgb, ccw_index, led_vertical_position,
)

log = logging.getLogger("sync")

# ── reactive-orchestra constants ─────────────────────────────────────────
IDLE_BASE_FACTOR  = 0.25      # never let panels go fully dark between notes
MELODY_FLASH_MS   = 200
MELODY_DECAY_MS   = 70
BASS_PULSE_MS     = 500
BASS_DECAY_MS     = 200
BASS_CYAN_SHIFT   = 0.40
HARMONY_WAVE_MS   = 250
OVERLAY_PERIOD_MS = 8000
OVERLAY_BASE      = 0.04
OVERLAY_BOOST     = 0.07
LOOP_QUIET_MS     = 2200
LOOP_PERIOD_MS    = 5000
LOOP_DEPTH        = 0.05
CYAN_RGB          = (0, 255, 220)

# ── dynamics (small range, motion is one step per played note) ───────────
IDLE_BRIGHTNESS  = 0.85
NOTE_BRIGHTNESS  = 1.00
BRI_RAMP_UP      = 0.30
BRI_RAMP_DOWN    = 0.10

# Per-panel flare colour comes from PALETTE[i].accent — each stepper gets
# its own identity (red / orange / yellow / magenta) on top of one shared
# blue base, see effects.py.
TRAIL_LEN        = 4
TRAIL_FADE       = 0.55
STEP_LERP        = 0.35
ONSET_STEP       = 1.0       # hard +1 LED tick on every note onset
DRIFT_LEDS_PER_S = 4.0       # continuous CW drift while a note is sustaining

# ── timing ───────────────────────────────────────────────────────────────
FRAME_RATE_HZ      = 12
FRAME_PERIOD       = 1.0 / FRAME_RATE_HZ
DEFAULT_OFFSET_MS  = 0       # GO handshake aligns start; offset is fine-tuning only
END_GRACE_MS       = 400
DEFAULT_PREROLL_MS = 2000    # panels show the effect, colour-tuned to the first
                             # note, this long BEFORE the song actually starts —
                             # gives a clean lead-in for video recording
PREROLL_FADE_MS    = 650     # smooth rise from black at the start of pre-roll


@dataclass
class PanelState:
    voice_idx: int                     # 0..3 — index into effects.PALETTE
    base_brightness: float = IDLE_BRIGHTNESS
    target_step: float     = 0.0       # +1 per onset, +drift while note sustains
    visual_pos: float      = 0.0       # animated toward target_step
    prev_active: bool      = False


@dataclass
class ReactiveState:
    """Per-panel state for the reactive-orchestra renderer."""
    voice_idx: int
    last_note_start_ms: int = -100000   # very long ago so first frame is idle
    last_note_dur_ms: int   = 0
    last_note_freq: float   = 0.0


# ── reactive layer functions ─────────────────────────────────────────────
def _melody_layer(voice, leds: list, dt_ms: int) -> list:
    if dt_ms > MELODY_FLASH_MS:
        return leds
    # Bright accent flash, exponential decay over the whole panel
    flash = math.exp(-dt_ms / MELODY_DECAY_MS)
    flash_color = scale_rgb(voice.accent, flash * 0.9)
    leds = [add_rgb(led, flash_color) for led in leds]
    # CCW chase head moves through 4 LEDs in MELODY_FLASH_MS
    chase_progress = dt_ms / MELODY_FLASH_MS         # 0..1
    head_step = chase_progress * 4
    for offset in range(4):
        idx = ccw_index(int(head_step) - offset)
        intensity = max(0.0, (1 - offset * 0.30)) * (1 - chase_progress)
        leds[idx] = add_rgb(leds[idx], scale_rgb(voice.main, intensity * 0.75))
    return leds


def _bass_layer(voice, leds: list, dt_ms: int) -> list:
    if dt_ms > BASS_PULSE_MS:
        return leds
    pulse = math.exp(-dt_ms / BASS_DECAY_MS)
    # Color shifts toward Melody's electric cyan during the pulse
    shifted = lerp_rgb(voice.main, CYAN_RGB, pulse * BASS_CYAN_SHIFT)
    pulse_color = scale_rgb(shifted, pulse * 0.85)
    return [add_rgb(led, pulse_color) for led in leds]


def _harmony_wave(voice, leds: list, dt_ms: int, going_up: bool) -> list:
    if dt_ms > HARMONY_WAVE_MS:
        return leds
    progress = dt_ms / HARMONY_WAVE_MS               # 0..1
    head_y = progress if going_up else (1.0 - progress)
    out = []
    for i, led in enumerate(leds):
        y = led_vertical_position(i)
        # gaussian-ish vertical envelope
        d = abs(y - head_y)
        wave = max(0.0, 1.0 - d * 5.0) * (1.0 - progress * 0.5)
        color = lerp_rgb(voice.main, voice.accent, wave * 0.6)
        out.append(add_rgb(led, scale_rgb(color, wave * 0.85)))
    return out


def _global_overlay(voice, leds: list, t_ms: int, dt_since_note: int) -> list:
    """Slow CCW chase shared by every panel; brightens briefly on note."""
    phase = (t_ms % OVERLAY_PERIOD_MS) / OVERLAY_PERIOD_MS
    head_step = phase * PANEL_LEDS
    head_idx = ccw_index(int(head_step))
    boost = OVERLAY_BOOST if dt_since_note < 200 else 0.0
    intensity = OVERLAY_BASE + boost
    head_color = scale_rgb(voice.accent, intensity)
    leds[head_idx] = add_rgb(leds[head_idx], head_color)
    # short trail (2 LEDs CCW)
    for off in (1, 2):
        idx = ccw_index(int(head_step) + off)
        fade = 0.5 ** off
        leds[idx] = add_rgb(leds[idx], scale_rgb(voice.accent, intensity * fade))
    return leds


def _loop_pulse(voice, leds: list, t_ms: int) -> list:
    """Subtle global breathing when nothing has played for a while."""
    phase = (t_ms % LOOP_PERIOD_MS) / LOOP_PERIOD_MS
    wave = 0.5 - 0.5 * math.cos(phase * 2 * math.pi)
    intensity = wave * LOOP_DEPTH
    pulse_color = scale_rgb(voice.main, intensity)
    return [add_rgb(led, pulse_color) for led in leds]


def _render_panel_reactive(state: ReactiveState, t_ms: int) -> list:
    voice = PALETTE[state.voice_idx]
    # Layer 1: idle base — deliberately NOT multiplied by base_brightness
    # so all four panels stay equally visible while idle. Hierarchy kicks
    # in via the reactive flash/wave layers, not by dimming the floor.
    base = scale_rgb(voice.main, IDLE_BASE_FACTOR)
    leds: list = [base] * PANEL_LEDS
    dt = t_ms - state.last_note_start_ms
    # Layer 2: voice-specific reactive
    if state.voice_idx == 0:
        leds = _melody_layer(voice, leds, dt)
    elif state.voice_idx == 1:
        leds = _bass_layer(voice, leds, dt)
    elif state.voice_idx == 2:
        leds = _harmony_wave(voice, leds, dt, going_up=True)
    elif state.voice_idx == 3:
        leds = _harmony_wave(voice, leds, dt, going_up=False)
    # Layer 3: global CCW overlay
    leds = _global_overlay(voice, leds, t_ms, dt)
    # Layer 4: loop pulse on extended silence
    if dt > LOOP_QUIET_MS:
        leds = _loop_pulse(voice, leds, t_ms)
    return leds


SYNC_RENDERERS = ("pulsewall", "wave", "reactive")
DEFAULT_RENDERER = "pulsewall"


# ═════════════════════════════════════════════════════════════════════════
# PULSE WALL — one unified, music-synced effect mirrored on ALL 4 panels.
#
# The whole wall acts as a single light instrument:
#   • base colour  = melody pitch  (neon violet→azure→cyan→magenta→pink ramp)
#   • wall brightness slam = bass onsets (the "thump")
#   • white-hot comet racing the ring = melody rhythm (motion = beat)
#   • shimmer fill = harmony voices (chords thicken the wall)
#   • near-black between hits = high contrast that pops on a phone screen
#   • slow neon breathing on long silences = never dead, clip-loop friendly
# All four panels render the identical frame, so the wall reads as one
# synchronised machine — the "how did they do that" hook for TikTok.
# ═════════════════════════════════════════════════════════════════════════

# Neon colour ramp keyed by melody pitch. Every stop keeps GREEN = 0, so
# any interpolated colour between two stops also has green 0 → fully
# saturated all the way through. This kills the washed-out near-white that
# appeared mid-ramp before, and keeps the purple/pink↔blue family the user
# liked. Only R and B move, the hue stays electric at every point.
_NEON_STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.00, (  0,   0, 255)),   # pure blue       (low notes)
    (0.28, ( 90,   0, 255)),   # blue-violet
    (0.52, (170,   0, 255)),   # violet
    (0.76, (255,   0, 235)),   # magenta
    (1.00, (255,   0, 150)),   # hot pink        (high notes)
)
_HUE_F_LO = 55.0      # A1 — bottom of the ramp
_HUE_F_HI = 1760.0    # A6 — top of the ramp

PUMP_DECAY_TAU_MS   = 190.0   # bass slam fall time (snappy = punchy)
WALL_BASE_CEIL      = 0.40    # dim background so the dots clearly dominate
HUE_EASE            = 0.34    # per-frame glide toward the melody's colour
HARM_EASE_UP        = 0.26
HARM_EASE_DN        = 0.06
BASS_FLOOR_EASE_UP  = 0.30
BASS_FLOOR_EASE_DN  = 0.10
RUNNER_DRIFT_LPS    = 7.0     # comet LEDs/sec while a melody note sustains
RUNNER_JUMP_LEDS    = 2.4     # hard comet advance per melody onset
PW_TRAIL_LEN        = 7       # comet trail length in LEDs
PW_TRAIL_TAU        = 1.7     # comet exponential falloff
WALL_FLOOR          = 0.03    # near-dark base between hits → contrast
BASS_SUSTAIN_FLOOR  = 0.34    # wall glow while a bass note is held
PW_IDLE_AFTER_MS    = 2400    # silence before the idle breath kicks in
PW_IDLE_BREATH_MS   = 4200
PW_IDLE_DEPTH       = 0.18
RUNNER_CORE         = (255, 255, 255)
RUNNER_CORE_MIX     = 0.15    # tiny white lift only at the very tip; the dot
                              # stays an intense, fully-saturated song colour
PW_BRIGHTNESS       = 0.55    # master cap on overall panel brightness during
                              # playback (1.0 = previous full brightness)


def _neon_rgb(freq: float) -> tuple[int, int, int]:
    """Map a note frequency to a saturated neon colour (log-pitch ramp)."""
    if freq <= 0:
        return _NEON_STOPS[0][1]
    t = math.log2(max(_HUE_F_LO, min(_HUE_F_HI, freq)) / _HUE_F_LO) / \
        math.log2(_HUE_F_HI / _HUE_F_LO)
    for (p0, c0), (p1, c1) in zip(_NEON_STOPS, _NEON_STOPS[1:]):
        if t <= p1:
            local = (t - p0) / (p1 - p0) if p1 > p0 else 0.0
            return lerp_rgb(c0, c1, local)
    return _NEON_STOPS[-1][1]


@dataclass
class PulseWallState:
    hue: tuple[float, float, float] = _NEON_STOPS[0][1]
    pump: float = 0.0               # bass slam envelope 0..1
    bass_floor: float = 0.0         # sustained-bass glow 0..1
    runner: float = 0.0             # comet position in LED-steps (CCW)
    harm: float = 0.0               # harmony energy 0..1
    last_activity_ms: int = -100000


def pulsewall_step(
    st: PulseWallState, dt_ms: int, *,
    melody_freq: Optional[float], melody_onsets: int,
    bass_onsets: int, bass_active: bool,
    harm_active_count: int, now_ms: int,
) -> None:
    """Advance PulseWall state by one frame. Pure: state in → state out."""
    dt = max(1, dt_ms)
    decay_p = math.exp(-dt / PUMP_DECAY_TAU_MS)

    st.pump *= decay_p
    if bass_onsets > 0:
        st.pump = 1.0
        st.last_activity_ms = now_ms

    bf_target = BASS_SUSTAIN_FLOOR if bass_active else 0.0
    ease_bf = BASS_FLOOR_EASE_UP if bass_active else BASS_FLOOR_EASE_DN
    st.bass_floor += (bf_target - st.bass_floor) * ease_bf

    if melody_freq is not None and melody_freq > 0:
        target = _neon_rgb(melody_freq)
        st.hue = tuple(h + (t - h) * HUE_EASE for h, t in zip(st.hue, target))
        st.runner += RUNNER_DRIFT_LPS * (dt / 1000.0)
        st.last_activity_ms = now_ms
    if melody_onsets > 0:
        st.runner += RUNNER_JUMP_LEDS * melody_onsets
        st.last_activity_ms = now_ms

    h_target = min(1.0, harm_active_count / 2.0)
    ease_h = HARM_EASE_UP if h_target > st.harm else HARM_EASE_DN
    st.harm += (h_target - st.harm) * ease_h
    if harm_active_count > 0:
        st.last_activity_ms = now_ms


def pulsewall_pixels(st: PulseWallState, now_ms: int) -> list:
    """Render one 16-LED panel from PulseWall state (mirrored on all panels)."""
    hue = (int(st.hue[0]), int(st.hue[1]), int(st.hue[2]))

    quiet = now_ms - st.last_activity_ms
    idle_breath = 0.0
    if quiet > PW_IDLE_AFTER_MS:
        ph = (now_ms % PW_IDLE_BREATH_MS) / PW_IDLE_BREATH_MS
        idle_breath = PW_IDLE_DEPTH * (0.5 - 0.5 * math.cos(ph * 2 * math.pi))

    # Colour glow is capped so the comet head stays the brightest thing on
    # the wall → during continuous parts the wall holds one colour while the
    # spinner reads clearly, with gentle bass-driven dynamics underneath.
    wall = min(WALL_BASE_CEIL, max(WALL_FLOOR, st.pump, st.bass_floor, idle_breath))
    base = scale_rgb(hue, wall)
    leds = [base] * PANEL_LEDS

    # The spinner: a bright comet of the *song colour* racing the ring CCW.
    # The head only tints partway toward white (RUNNER_CORE_MIX) so it never
    # flashes — it stays a luminous accent of the same hue.
    head = st.runner
    for led in range(PANEL_LEDS):
        step = (-led) % PANEL_LEDS                 # this LED's CCW step
        d = (head - step) % PANEL_LEDS             # steps behind the head
        if d <= PW_TRAIL_LEN:
            inten = math.exp(-d / PW_TRAIL_TAU)
            white_mix = RUNNER_CORE_MIX * max(0.0, 1.0 - d / 2.0)
            core = lerp_rgb(hue, RUNNER_CORE, white_mix)
            leds[led] = add_rgb(leds[led], scale_rgb(core, inten * (1.0 + 0.4 * st.pump)))

    # Harmony shimmer — subtle same-hue sparkle so chords "thicken" the wall.
    if st.harm > 0.02:
        bucket = now_ms // 70
        tint = hue   # pure saturated song colour, no white
        for led in range(PANEL_LEDS):
            if ((led * 2654435761) ^ (bucket * 40503)) % 5 == 0:
                leds[led] = add_rgb(leds[led], scale_rgb(tint, 0.16 * st.harm))

    return [scale_rgb(c, PW_BRIGHTNESS) for c in leds]


def _build_voice_timeline(events: list[dict]) -> list[tuple[int, int, float]]:
    t = 0
    spans: list[tuple[int, int, float]] = []
    for e in events:
        d = int(e["d"])
        f = float(e["f"])
        if f > 0:
            spans.append((t, t + d, f))
        t += d
    return spans


def _find_active(timeline: list[tuple[int, int, float]], now_ms: int):
    for s, e, f in timeline:
        if s <= now_ms < e:
            return f
    return None


class SyncEngine:
    def __init__(self, bridge_url: str) -> None:
        self.bridge = httpx.AsyncClient(base_url=bridge_url, timeout=2.0)
        self._task: Optional[asyncio.Task] = None
        self._stop_flag = False
        self._current_song: Optional[str] = None
        self._go_event = asyncio.Event()
        self._renderer_name = DEFAULT_RENDERER
        self._preroll_ms = 0

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def preroll_ms(self) -> int:
        """Lead-in the caller should wait before releasing the player GO."""
        return self._preroll_ms

    @property
    def current_song(self) -> Optional[str]:
        return self._current_song if self.active else None

    async def warm_up(self) -> None:
        """Open the LED-bridge serial via the first idle frame.

        The bridge pays a ~1.5s DTR-settle on its first send; doing it here
        lets the timing-critical frame loop start without that latency.
        Sends an all-black frame so the wall stays dark until the renderer
        (e.g. the pulsewall pre-roll) fades the effect in cleanly.
        """
        dark = "000000" * (NUM_PANELS * PANEL_LEDS)
        await self._send_line(f"frame:{dark}")

    def trigger_go(self) -> None:
        """Release the sync loop from its GO-wait. Pair with bridge POST /go."""
        self._go_event.set()

    async def start(self, voices: dict, stem: Optional[str] = None,
                    offset_ms: int = DEFAULT_OFFSET_MS,
                    renderer: str = DEFAULT_RENDERER,
                    preroll_ms: int = DEFAULT_PREROLL_MS) -> None:
        await self.stop(restore_mode=False)
        self._stop_flag = False
        self._go_event.clear()
        self._current_song = stem
        self._renderer_name = renderer if renderer in SYNC_RENDERERS else DEFAULT_RENDERER
        # Pre-roll is only implemented for pulsewall; others start as before.
        self._preroll_ms = max(0, preroll_ms) if self._renderer_name == "pulsewall" else 0
        self._task = asyncio.create_task(self._run(voices, offset_ms))
        log.info("sync queued stem=%s offset=%dms renderer=%s preroll=%dms (waiting for GO)",
                 stem, offset_ms, self._renderer_name, self._preroll_ms)

    async def stop(self, restore_mode: bool = True) -> None:
        if self._task and not self._task.done():
            self._stop_flag = True
            try:
                await asyncio.wait_for(self._task, timeout=1.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None
        self._current_song = None
        if restore_mode:
            await self._send_line("mode:infinity")

    async def _send_line(self, line: str) -> bool:
        try:
            await self.bridge.post("/command", json={"line": line}, timeout=0.4)
            return True
        except Exception as e:
            log.debug("bridge send failed: %s", e)
            return False

    def _render_panel(self, p: PanelState) -> str:
        """Shared blue base + per-panel flare colour ticking CW one step per note."""
        voice = PALETTE[p.voice_idx]
        flare = voice.accent                # this panel's spark colour
        base_color = scale_rgb(voice.main, p.base_brightness)
        leds: list = [base_color] * PANEL_LEDS

        head_int = int(p.visual_pos) % PANEL_LEDS
        head_frac = p.visual_pos - int(p.visual_pos)

        # Sub-LED interpolation between the LED the flare is leaving and
        # the LED it's entering. Direction is CW: indices increase.
        leaving_led = head_int
        entering_led = (head_int + 1) % PANEL_LEDS
        leds[leaving_led]  = lerp_rgb(base_color, flare, 1.0 - head_frac * 0.3)
        leds[entering_led] = lerp_rgb(leds[entering_led], flare, head_frac * 0.85)

        # Trail behind the head — CW means previous indices
        for off in range(1, TRAIL_LEN + 1):
            trail_led = (head_int - off) % PANEL_LEDS
            fade = math.pow(TRAIL_FADE, off)
            leds[trail_led] = lerp_rgb(leds[trail_led], flare, fade)

        return "".join(rgb_to_hex(led) for led in leds)

    def _build_frame_hex(self, panels: list[PanelState]) -> str:
        return "".join(self._render_panel(p) for p in panels)

    async def _run(self, voices: dict, offset_ms: int) -> None:
        timelines = [
            _build_voice_timeline(voices.get(f"voice{i+1}", []))
            for i in range(NUM_PANELS)
        ]
        song_end_ms = max(
            (max((e for _, e, _ in tl), default=0) for tl in timelines), default=0
        )
        if song_end_ms == 0:
            log.info("sync: empty song, nothing to do")
            return

        await self._go_event.wait()
        log.info("sync GO received — renderer=%s", self._renderer_name)

        loop = asyncio.get_event_loop()
        if offset_ms >= 0:
            await asyncio.sleep(offset_ms / 1000.0)
            t0 = loop.time()
        else:
            t0 = loop.time() + (offset_ms / 1000.0)

        if self._renderer_name == "reactive":
            await self._loop_reactive(timelines, song_end_ms, t0)
        elif self._renderer_name == "wave":
            await self._loop_wave(timelines, song_end_ms, t0)
        else:
            await self._loop_pulsewall(timelines, song_end_ms, t0)

        await self._send_line("mode:infinity")
        log.info("sync finished")

    async def _loop_pulsewall(self, timelines, song_end_ms, t0) -> None:
        """One unified music-synced effect, identical on all 4 panels.

        Melody (v1) drives colour + the ring comet, bass (v2) slams wall
        brightness, harmony (v3+v4) adds shimmer. See PULSE WALL header.
        """
        loop = asyncio.get_event_loop()
        st = PulseWallState()
        last_ms = -1
        prev_ms = 0

        def _onsets(tl, lo, hi):
            return sum(1 for s, _, _ in tl if lo < s <= hi)

        # ── pre-roll ─────────────────────────────────────────────────────
        # Colour-tune the wall to the first note and run the spinner for
        # self._preroll_ms BEFORE the song starts, so a video capture has a
        # clean lead-in instead of the panels snapping on the first note.
        # The caller holds back the player GO for the same duration.
        first_freq = 0.0
        for tl in timelines:
            if tl:
                first_freq = tl[0][2]
                break
        if first_freq > 0:
            st.hue = _neon_rgb(first_freq)

        if self._preroll_ms > 0:
            pre_t0 = loop.time()
            pre_prev = 0
            while not self._stop_flag:
                el = int((loop.time() - pre_t0) * 1000)
                if el >= self._preroll_ms:
                    break
                dt = max(1, el - pre_prev)
                pre_prev = el
                pulsewall_step(
                    st, dt, melody_freq=(first_freq or None), melody_onsets=0,
                    bass_onsets=0, bass_active=False,
                    harm_active_count=0, now_ms=el,
                )
                fade = min(1.0, el / PREROLL_FADE_MS)
                px = [scale_rgb(c, fade) for c in pulsewall_pixels(st, el)]
                panel_hex = "".join(rgb_to_hex(c) for c in px)
                await self._send_line(f"frame:{panel_hex * NUM_PANELS}")
                await asyncio.sleep(FRAME_PERIOD)

        # Song clock starts now; the first note sounds because the caller
        # released the player GO after the same pre-roll wait.
        t0 = loop.time()
        last_ms = -1
        prev_ms = 0

        while not self._stop_flag:
            now_ms = int((loop.time() - t0) * 1000)
            dt_ms = now_ms - prev_ms
            prev_ms = now_ms

            mel_on = _onsets(timelines[0], last_ms, now_ms)
            bass_on = _onsets(timelines[1], last_ms, now_ms)
            mel_f = _find_active(timelines[0], now_ms)
            bass_a = _find_active(timelines[1], now_ms) is not None
            harm_n = sum(
                1 for i in (2, 3)
                if i < len(timelines) and _find_active(timelines[i], now_ms) is not None
            )
            last_ms = now_ms

            pulsewall_step(
                st, dt_ms,
                melody_freq=mel_f, melody_onsets=mel_on,
                bass_onsets=bass_on, bass_active=bass_a,
                harm_active_count=harm_n, now_ms=now_ms,
            )
            panel_hex = "".join(rgb_to_hex(c) for c in pulsewall_pixels(st, now_ms))
            await self._send_line(f"frame:{panel_hex * NUM_PANELS}")

            if now_ms > song_end_ms + END_GRACE_MS:
                break
            await asyncio.sleep(FRAME_PERIOD)

        # graceful fade — let everything decay toward the idle breath
        for _ in range(24):
            now_ms = int((loop.time() - t0) * 1000)
            dt_ms = now_ms - prev_ms
            prev_ms = now_ms
            pulsewall_step(
                st, dt_ms, melody_freq=None, melody_onsets=0,
                bass_onsets=0, bass_active=False,
                harm_active_count=0, now_ms=now_ms,
            )
            panel_hex = "".join(rgb_to_hex(c) for c in pulsewall_pixels(st, now_ms))
            await self._send_line(f"frame:{panel_hex * NUM_PANELS}")
            await asyncio.sleep(FRAME_PERIOD)

    async def _loop_wave(self, timelines, song_end_ms, t0) -> None:
        """Berry-wine panels with a yellow CW flare that ticks one step per note.

        Brightness only varies in [IDLE_BRIGHTNESS, NOTE_BRIGHTNESS]. The
        flare rotates clockwise and only advances when a note onset fires,
        so the wall reads the rhythm directly.
        """
        loop = asyncio.get_event_loop()
        panels = [PanelState(voice_idx=i) for i in range(NUM_PANELS)]
        last_check_ms = -1

        while not self._stop_flag:
            now_ms = int((loop.time() - t0) * 1000)

            # Detect note onsets that occurred since the previous frame
            for i, p in enumerate(panels):
                for s, _, _ in timelines[i]:
                    if last_check_ms < s <= now_ms:
                        p.target_step += ONSET_STEP   # hard tick per note start
                freq = _find_active(timelines[i], now_ms)
                is_active = freq is not None

                # While the note is sustaining, the flare keeps drifting CW.
                # On note-off it stops immediately, so silences read as still.
                if is_active:
                    p.target_step += DRIFT_LEDS_PER_S * FRAME_PERIOD

                target_bri = NOTE_BRIGHTNESS if is_active else IDLE_BRIGHTNESS
                ramp = BRI_RAMP_UP if is_active else BRI_RAMP_DOWN
                p.base_brightness += (target_bri - p.base_brightness) * ramp

                # Ease the flare from its previous position toward target_step
                p.visual_pos += (p.target_step - p.visual_pos) * STEP_LERP
                p.prev_active = is_active

            last_check_ms = now_ms
            await self._send_line(f"frame:{self._build_frame_hex(panels)}")
            if now_ms > song_end_ms + END_GRACE_MS:
                break
            await asyncio.sleep(FRAME_PERIOD)

        # graceful fade: brightness rolls back to idle, flare settles on its last step
        for _ in range(20):
            for p in panels:
                p.base_brightness += (IDLE_BRIGHTNESS - p.base_brightness) * BRI_RAMP_DOWN
                p.visual_pos += (p.target_step - p.visual_pos) * STEP_LERP
            await self._send_line(f"frame:{self._build_frame_hex(panels)}")
            await asyncio.sleep(FRAME_PERIOD)

    async def _loop_reactive(self, timelines, song_end_ms, t0) -> None:
        """Reactive Gear Orchestra — note-event-driven layered rendering."""
        loop = asyncio.get_event_loop()
        states = [ReactiveState(voice_idx=i) for i in range(NUM_PANELS)]
        last_check_ms = -1
        while not self._stop_flag:
            now_ms = int((loop.time() - t0) * 1000)
            # Detect note onsets that occurred since the previous frame check
            for i, tl in enumerate(timelines):
                for s, e, f in tl:
                    if last_check_ms < s <= now_ms:
                        states[i].last_note_start_ms = s
                        states[i].last_note_dur_ms = e - s
                        states[i].last_note_freq = f
            last_check_ms = now_ms

            # Render layered frame
            frame_hex = "".join(
                "".join(rgb_to_hex(led) for led in _render_panel_reactive(st, now_ms))
                for st in states
            )
            await self._send_line(f"frame:{frame_hex}")
            if now_ms > song_end_ms + END_GRACE_MS:
                break
            await asyncio.sleep(FRAME_PERIOD)

        # graceful fade-out: just let dt grow and idle base shines through
        fade_t0 = loop.time()
        for _ in range(20):
            now_ms = int((loop.time() - t0) * 1000)
            frame_hex = "".join(
                "".join(rgb_to_hex(led) for led in _render_panel_reactive(st, now_ms))
                for st in states
            )
            await self._send_line(f"frame:{frame_hex}")
            await asyncio.sleep(FRAME_PERIOD)
