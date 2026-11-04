"""High-level converter: MIDI file in, Arduino song header out.

Wraps the whole pipeline (parse, quantize, arrange, export) in one function
that returns a structured result for the web backend.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any

from .midi_parser import parse_midi_file
from .quantizer import quantize_events, merge_consecutive_slices
from .arranger import (
    arrange_four_voices,
    apply_octave_shift,
    clamp_to_range,
    voice_slices_to_arduino_events,
)
from .exporter_arduino import export_song_header


@dataclass
class ConvertOptions:
    quantize_division: int = 16
    bpm: Optional[float] = None
    min_midi: int = 48
    max_midi: int = 96
    octave_shift: int = 0
    clamp: bool = True
    min_duration_ms: int = 0
    drop_drums: bool = True


@dataclass
class ConvertResult:
    success: bool
    output_path: Optional[str] = None
    voice1_events: int = 0
    voice2_events: int = 0
    voice3_events: int = 0
    voice4_events: int = 0
    total_events: int = 0
    bytes_progmem: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


def compute_grid_ms(quantize_division: int, bpm: float) -> int:
    wholenote_ms = 60000.0 / bpm * 4.0
    return max(1, int(round(wholenote_ms / quantize_division)))


def convert_midi(
    input_path: str,
    output_path: str,
    options: Optional[ConvertOptions] = None,
    song_name: Optional[str] = None,
) -> ConvertResult:
    opts = options or ConvertOptions()

    try:
        events, meta = parse_midi_file(input_path, drop_drums=opts.drop_drums)
    except Exception as exc:
        return ConvertResult(success=False, error=f"parse failed: {exc}")

    if not events:
        return ConvertResult(success=False, error="no note events after parsing")

    bpm = opts.bpm if opts.bpm else (meta.get("initial_tempo_bpm") or 120.0)
    grid_ms = compute_grid_ms(opts.quantize_division, bpm)

    try:
        events_q = quantize_events(events, grid_ms=grid_ms,
                                   min_duration_ms=opts.min_duration_ms)

        v1, v2, v3, v4 = arrange_four_voices(events_q, grid_ms=grid_ms)

        if opts.octave_shift != 0:
            v1 = apply_octave_shift(v1, opts.octave_shift)
            v2 = apply_octave_shift(v2, opts.octave_shift)
            v3 = apply_octave_shift(v3, opts.octave_shift)
            v4 = apply_octave_shift(v4, opts.octave_shift)

        if opts.clamp:
            v1 = clamp_to_range(v1, opts.min_midi, opts.max_midi, octave_wrap=True)
            v2 = clamp_to_range(v2, opts.min_midi, opts.max_midi, octave_wrap=True)
            v3 = clamp_to_range(v3, opts.min_midi, opts.max_midi, octave_wrap=True)
            v4 = clamp_to_range(v4, opts.min_midi, opts.max_midi, octave_wrap=True)

        v1 = merge_consecutive_slices(v1)
        v2 = merge_consecutive_slices(v2)
        v3 = merge_consecutive_slices(v3)
        v4 = merge_consecutive_slices(v4)

        v1_ard = voice_slices_to_arduino_events(v1)
        v2_ard = voice_slices_to_arduino_events(v2)
        v3_ard = voice_slices_to_arduino_events(v3)
        v4_ard = voice_slices_to_arduino_events(v4)

        stem = song_name or Path(input_path).stem
        header_meta = {
            "bpm_used": bpm,
            "quantize": f"1/{opts.quantize_division}",
            "midi_tempo_changes": meta["tempo_changes"],
            "midi_duration_s": f"{meta['length_seconds']:.2f}",
        }
        export_song_header(
            output_path,
            v1_ard, v2_ard, v3_ard, v4_ard,
            song_name=stem,
            source_file=input_path,
            metadata=header_meta,
        )
    except Exception as exc:
        return ConvertResult(success=False, error=f"conversion failed: {exc}")

    total = len(v1_ard) + len(v2_ard) + len(v3_ard) + len(v4_ard)
    return ConvertResult(
        success=True,
        output_path=output_path,
        voice1_events=len(v1_ard),
        voice2_events=len(v2_ard),
        voice3_events=len(v3_ard),
        voice4_events=len(v4_ard),
        total_events=total,
        bytes_progmem=total * 6,
        metadata={
            "duration_seconds": meta["length_seconds"],
            "num_tracks": meta["num_tracks"],
            "num_raw_events": meta["num_events"],
            "channels_used": meta["channels_used"],
            "initial_tempo_bpm": meta["initial_tempo_bpm"],
            "tempo_changes": meta["tempo_changes"],
            "bpm_used": bpm,
            "grid_ms": grid_ms,
        },
    )
