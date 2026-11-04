"""Core data types of the pipeline.

All times in absolute milliseconds. Notes as MIDI note numbers
(0-127, 69 = A4 = 440 Hz). Frequencies in Hz.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class RawNoteEvent:
    """A note-on/note-off pair from a MIDI file, in milliseconds."""
    start_ms: int
    end_ms: int
    midi_note: int
    velocity: int
    track: int
    channel: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass
class VoiceSlice:
    """One time window of a voice after arrangement.

    `midi_note is None` means a rest in this window.
    """
    start_ms: int
    end_ms: int
    midi_note: Optional[int]

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass
class ArduinoNoteEvent:
    """Final event for the Arduino engine. Frequency 0.0 = rest."""
    frequency: float
    duration_ms: int
