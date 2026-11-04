"""Helpers."""


def midi_note_to_frequency(midi_note: int) -> float:
    """Convert a MIDI note number to a frequency in Hz.

    Standard formula, A4 (MIDI 69) = 440 Hz.
    """
    return 440.0 * (2.0 ** ((midi_note - 69) / 12.0))


def frequency_to_midi_note(freq: float) -> int:
    """Inverse: nearest MIDI note for a frequency (for debugging)."""
    if freq <= 0:
        return -1
    import math
    return round(69 + 12 * math.log2(freq / 440.0))
