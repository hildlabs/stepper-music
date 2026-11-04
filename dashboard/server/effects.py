"""LED effect library for the 4-panel wall.

Each effect renders a single 4-panel × 16-LED frame for a given
monotonic time `t_ms`. Effects are stateless w.r.t. the engine; any
internal state (e.g. wave phase) is computed from `t_ms`.

Physical LED layout per panel (data-pin first LED is index 0):
    LED  0..3   bottom edge,  left → right
    LED  4..7   right edge,   bottom → top
    LED  8..11  top edge,     right → left
    LED 12..15  left edge,    top → bottom

The stepper gears spin counter-clockwise. Looking at the panel face,
CCW is: bottom-right → bottom-left → top-left → top-right → loop.
With the indexing above that is: 3 → 0 → 15 → 12 → 11 → 8 → 7 → 4 → 3.
Mathematically: ccw_index(k) = (16 - k) % 16 walks the ring CCW from
LED 0 onwards.

Palette — all four panels share the same deep blue base, all four flares
share the same punchy red. Reads as one cohesive wall with red ticks
moving CW around each panel.
    All panels   main   (  0, 60,255) saturated deep blue
    All panels   accent (255, 20, 20) punchy red
"""
from __future__ import annotations

import math
from dataclasses import dataclass

PANEL_LEDS = 16
NUM_PANELS = 4

RGB = tuple[int, int, int]


@dataclass(frozen=True)
class VoicePalette:
    name: str
    main: RGB
    accent: RGB
    base_brightness: float   # 0..1 — per-voice brightness bias


PANEL_BLUE  = (  0,  60, 255)   # sat deep blue, almost no red
FLARE_RED   = (255,  20,  20)   # uniform punchy red flare on all panels

PALETTE: tuple[VoicePalette, ...] = (
    VoicePalette("melody",     PANEL_BLUE, FLARE_RED, 1.0),
    VoicePalette("bass",       PANEL_BLUE, FLARE_RED, 1.0),
    VoicePalette("harmony_up", PANEL_BLUE, FLARE_RED, 1.0),
    VoicePalette("harmony_dn", PANEL_BLUE, FLARE_RED, 1.0),
)

# Brightness cap — full range now that the 5V supply has plenty of headroom.
MAX_BRIGHTNESS = 255

# Linear (no gamma compression) — RGB values map directly to LED output,
# colours stay punchy at idle brightness.
GAMMA = 1.0


# ── helpers ──────────────────────────────────────────────────────────────
def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * clamp(t)


def lerp_rgb(a: RGB, b: RGB, t: float) -> RGB:
    return (
        int(lerp(a[0], b[0], t)),
        int(lerp(a[1], b[1], t)),
        int(lerp(a[2], b[2], t)),
    )


def scale_rgb(c: RGB, factor: float) -> RGB:
    f = clamp(factor, 0.0, 4.0)  # allow boost above 1.0 before final cap
    return (int(c[0] * f), int(c[1] * f), int(c[2] * f))


def add_rgb(a: RGB, b: RGB) -> RGB:
    """Additive blend with clamp — used for layering reactive effects."""
    return (
        min(255, a[0] + b[0]),
        min(255, a[1] + b[1]),
        min(255, a[2] + b[2]),
    )


def led_vertical_position(led: int) -> float:
    """0 (bottom) .. 1 (top) for a given LED index on the ring."""
    if led <= 3:
        return 0.0                       # bottom edge
    if led <= 7:
        return (led - 3) / 4.0           # right edge bottom→top: 0.25 .. 1.0
    if led <= 11:
        return 1.0                       # top edge
    return 1.0 - ((led - 11) / 4.0)      # left edge top→bottom: 0.75 .. 0.0


def apply_gamma_and_cap(c: RGB) -> RGB:
    """Gamma-correct then cap at MAX_BRIGHTNESS so the camera stays sane."""
    cap = MAX_BRIGHTNESS / 255.0
    out = []
    for ch in c:
        normalized = clamp(ch / 255.0)
        corrected = math.pow(normalized, GAMMA)
        out.append(int(corrected * cap * 255))
    return (out[0], out[1], out[2])


def rgb_to_hex(c: RGB) -> str:
    final = apply_gamma_and_cap(c)
    return f"{final[0]:02x}{final[1]:02x}{final[2]:02x}"


# ── ring geometry ────────────────────────────────────────────────────────
def ccw_index(step: int) -> int:
    """Convert a step count (0,1,2,...) to a ring LED index walking CCW.

    step=0 → 0, step=1 → 15, step=2 → 14, ..., step=15 → 1, step=16 → 0.
    """
    return (-step) % PANEL_LEDS


def ring_distance(a: int, b: int) -> int:
    """Shortest distance between two LED indices on the 16-ring."""
    d = abs(a - b) % PANEL_LEDS
    return min(d, PANEL_LEDS - d)


# ── frame rendering ──────────────────────────────────────────────────────
def build_frame(panel_leds: list[list[RGB]]) -> str:
    """Take 4 lists of 16 RGB tuples and emit the bridge's frame:<hex>."""
    out: list[str] = []
    for panel in panel_leds:
        for led in panel:
            out.append(rgb_to_hex(led))
    return "".join(out)


# ── effect base ──────────────────────────────────────────────────────────
class Effect:
    """Base class. Render a frame for time `t_ms`."""
    name: str = "base"
    label: str = "Base"

    def render(self, t_ms: int) -> list[list[RGB]]:
        return [[(0, 0, 0)] * PANEL_LEDS for _ in range(NUM_PANELS)]
