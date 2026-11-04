"""Quantizing and merging.

1. Snap RawNoteEvent times to a grid (e.g. 1/16 notes).
2. Optionally drop very short notes.
3. Merge consecutive VoiceSlices with the same note.
4. Merge consecutive rests as well.
"""

from typing import List

from .music_types import RawNoteEvent, VoiceSlice


def quantize_events(
    events: List[RawNoteEvent],
    grid_ms: int,
    min_duration_ms: int = 0,
) -> List[RawNoteEvent]:
    """Snap start/end of all events to a grid of grid_ms.

    Notes shorter than min_duration_ms after snapping are dropped.
    """
    if grid_ms <= 0:
        return list(events)

    out: List[RawNoteEvent] = []
    for e in events:
        start = (e.start_ms // grid_ms) * grid_ms
        end = ((e.end_ms + grid_ms - 1) // grid_ms) * grid_ms  # round up
        if end <= start:
            end = start + grid_ms
        if (end - start) < min_duration_ms:
            continue
        out.append(RawNoteEvent(
            start_ms=start, end_ms=end,
            midi_note=e.midi_note, velocity=e.velocity,
            track=e.track, channel=e.channel,
        ))
    return out


def merge_consecutive_slices(slices: List[VoiceSlice]) -> List[VoiceSlice]:
    """Merge adjacent VoiceSlices with the same note (or both rests)."""
    if not slices:
        return []

    merged: List[VoiceSlice] = [slices[0]]
    for s in slices[1:]:
        last = merged[-1]
        # Adjacent (end == start) AND same note (including both None)
        if s.start_ms == last.end_ms and s.midi_note == last.midi_note:
            merged[-1] = VoiceSlice(
                start_ms=last.start_ms,
                end_ms=s.end_ms,
                midi_note=last.midi_note,
            )
        else:
            merged.append(s)
    return merged
