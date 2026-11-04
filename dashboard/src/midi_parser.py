"""MIDI parser: reads a .mid file and extracts clean note events.

- Applies tempo changes to the timeline (a MIDI file may contain many)
- Pairs note-on/note-off (note-on with velocity 0 counts as off)
- Optionally drops the drum channel (MIDI channel 10, 0-indexed: 9)
- Returns absolute times in milliseconds
"""

from typing import List, Tuple, Dict, Any
import mido

from .music_types import RawNoteEvent


DRUM_CHANNEL = 9  # MIDI channel 10, 0-indexed


def parse_midi_file(
    path: str,
    drop_drums: bool = True,
) -> Tuple[List[RawNoteEvent], Dict[str, Any]]:
    """Parse a MIDI file.

    Returns (events, metadata). events is sorted by start_ms.
    metadata includes track count, duration in seconds and channels used.

    Tempo: mido merges all tracks by absolute tick time. We walk the delta
    ticks and accumulate absolute time ourselves, with `set_tempo` events
    changing the conversion factor, so tempo ramps come out right too.
    """
    mid = mido.MidiFile(path)
    ticks_per_beat = mid.ticks_per_beat

    merged = mido.merge_tracks(mid.tracks)

    events: List[RawNoteEvent] = []
    abs_seconds = 0.0
    tempo = 500000  # default: 120 BPM (500000 us per quarter)

    # (channel, note) -> (start_ms, velocity)
    # Track assignment is ignored on purpose, the arranger works across
    # tracks anyway. track=0 is a placeholder.
    open_notes: Dict[Tuple[int, int], Tuple[int, int]] = {}

    channels_seen = set()
    tempo_changes = 0

    for msg in merged:
        delta_s = mido.tick2second(msg.time, ticks_per_beat, tempo)
        abs_seconds += delta_s
        abs_ms = int(round(abs_seconds * 1000))

        if msg.type == "set_tempo":
            tempo = msg.tempo
            tempo_changes += 1
            continue

        if msg.type == "note_on" and msg.velocity > 0:
            if drop_drums and msg.channel == DRUM_CHANNEL:
                continue
            channels_seen.add(msg.channel)
            key = (msg.channel, msg.note)
            # Already open (double note-on): close the old one first.
            if key in open_notes:
                start_ms, vel = open_notes.pop(key)
                if abs_ms > start_ms:
                    events.append(RawNoteEvent(
                        start_ms=start_ms, end_ms=abs_ms,
                        midi_note=msg.note, velocity=vel,
                        track=0, channel=msg.channel,
                    ))
            open_notes[key] = (abs_ms, msg.velocity)

        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            if drop_drums and msg.channel == DRUM_CHANNEL:
                continue
            key = (msg.channel, msg.note)
            if key in open_notes:
                start_ms, vel = open_notes.pop(key)
                if abs_ms > start_ms:
                    events.append(RawNoteEvent(
                        start_ms=start_ms, end_ms=abs_ms,
                        midi_note=msg.note, velocity=vel,
                        track=0, channel=msg.channel,
                    ))

    # Close notes still open at the end.
    final_ms = int(round(abs_seconds * 1000))
    for (channel, note), (start_ms, vel) in open_notes.items():
        if final_ms > start_ms:
            events.append(RawNoteEvent(
                start_ms=start_ms, end_ms=final_ms,
                midi_note=note, velocity=vel,
                track=0, channel=channel,
            ))

    events.sort(key=lambda e: (e.start_ms, e.midi_note))

    metadata = {
        "filename": path,
        "ticks_per_beat": ticks_per_beat,
        "num_tracks": len(mid.tracks),
        "length_seconds": mid.length,
        "num_events": len(events),
        "channels_used": sorted(channels_seen),
        "tempo_changes": tempo_changes,
        "initial_tempo_bpm": round(60000000 / tempo, 1) if tempo else None,
    }

    return events, metadata
