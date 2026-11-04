"""Arrangement onto 4 voices.

Time-based, NOT "track 1 = voice 1":
- Split the song into discrete time windows (grid_ms).
- For each window, collect all active notes (a note is "active" if its
  interval overlaps the window).
- Distribute the notes onto 4 voices:
    voice1 = highest active note   (melody, X motor)
    voice2 = lowest active note    (bass, Y motor)
    voice3 = upper harmony         (Z motor)
    voice4 = lower harmony         (A motor, D12/D13)
  If fewer than 4 distinct notes are active, the remaining voices rest (None).

This version deliberately has no voice leading (it does not try to keep the
same note on the same voice over time). Merging happens afterwards in the
quantizer via merge_consecutive_slices.
"""

from typing import List, Tuple, Optional

from .music_types import RawNoteEvent, VoiceSlice, ArduinoNoteEvent
from .utils import midi_note_to_frequency


def arrange_four_voices(
    events: List[RawNoteEvent],
    grid_ms: int,
) -> Tuple[List[VoiceSlice], List[VoiceSlice], List[VoiceSlice], List[VoiceSlice]]:
    """Time-based 4-voice arrangement. See module docstring."""
    if not events:
        return [], [], [], []

    song_end = max(e.end_ms for e in events)

    # Sort by start (should already be sorted, just to be safe)
    events_sorted = sorted(events, key=lambda e: (e.start_ms, e.midi_note))

    v1: List[VoiceSlice] = []
    v2: List[VoiceSlice] = []
    v3: List[VoiceSlice] = []
    v4: List[VoiceSlice] = []

    # Sweep: find the active notes for every grid window.
    # Events are sorted by start time, so one moving index is enough.
    idx = 0
    active: List[RawNoteEvent] = []  # currently sounding events

    t = 0
    while t < song_end:
        window_start = t
        window_end = t + grid_ms

        # Add events that start in this window
        while idx < len(events_sorted) and events_sorted[idx].start_ms < window_end:
            active.append(events_sorted[idx])
            idx += 1
        # Drop events that have ended
        active = [e for e in active if e.end_ms > window_start]

        # Active MIDI notes as a unique set
        notes = sorted({e.midi_note for e in active})

        v1_note, v2_note, v3_note, v4_note = assign_voices(notes)

        v1.append(VoiceSlice(window_start, window_end, v1_note))
        v2.append(VoiceSlice(window_start, window_end, v2_note))
        v3.append(VoiceSlice(window_start, window_end, v3_note))
        v4.append(VoiceSlice(window_start, window_end, v4_note))

        t = window_end

    return v1, v2, v3, v4


def assign_voices(
    sorted_notes: List[int],
) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """Distribute a sorted list of notes onto (voice1, voice2, voice3, voice4).
       voice1 = highest, voice2 = lowest,
       voice3 = upper harmony (second highest),
       voice4 = lower harmony (second lowest).
       Missing voices -> None (rest).

    Taking second highest and second lowest keeps audible intervals between
    the motors. Close frequencies on two different steppers produce an
    unpleasant beating.
    """
    n = len(sorted_notes)
    if n == 0:
        return None, None, None, None
    if n == 1:
        return sorted_notes[0], None, None, None
    if n == 2:
        return sorted_notes[-1], sorted_notes[0], None, None
    if n == 3:
        # Three notes: highest, lowest, middle. voice3 gets the middle one,
        # voice4 stays silent.
        return sorted_notes[-1], sorted_notes[0], sorted_notes[1], None
    # n >= 4: highest, lowest, second-highest, second-lowest
    highest = sorted_notes[-1]
    lowest = sorted_notes[0]
    upper_harmony = sorted_notes[-2]
    lower_harmony = sorted_notes[1]
    return highest, lowest, upper_harmony, lower_harmony


def apply_octave_shift(slices: List[VoiceSlice], octaves: int) -> List[VoiceSlice]:
    if octaves == 0:
        return slices
    out = []
    for s in slices:
        new_note = s.midi_note + 12 * octaves if s.midi_note is not None else None
        out.append(VoiceSlice(s.start_ms, s.end_ms, new_note))
    return out


def clamp_to_range(
    slices: List[VoiceSlice],
    min_note: int,
    max_note: int,
    octave_wrap: bool = True,
) -> List[VoiceSlice]:
    """Force notes into the range [min_note, max_note].

    octave_wrap=True: shift by octaves until the note fits (sounds smoother).
    octave_wrap=False: out-of-range notes become rests.
    """
    out = []
    for s in slices:
        n = s.midi_note
        if n is None:
            out.append(s)
            continue
        if octave_wrap:
            while n < min_note:
                n += 12
            while n > max_note:
                n -= 12
            if n < min_note or n > max_note:
                n = None  # did not fit
        else:
            if n < min_note or n > max_note:
                n = None
        out.append(VoiceSlice(s.start_ms, s.end_ms, n))
    return out


def voice_slices_to_arduino_events(slices: List[VoiceSlice]) -> List[ArduinoNoteEvent]:
    out: List[ArduinoNoteEvent] = []
    for s in slices:
        if s.midi_note is None:
            freq = 0.0
        else:
            freq = midi_note_to_frequency(s.midi_note)
        out.append(ArduinoNoteEvent(frequency=freq, duration_ms=s.duration_ms))
    return out
