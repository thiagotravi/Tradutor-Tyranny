_BIDI_CONTROL_CHARS = (
    "\u200e"  # LRM
    "\u200f"  # RLM
    "\u202a"  # LRE
    "\u202b"  # RLE
    "\u202c"  # PDF
    "\u202d"  # LRO
    "\u202e"  # RLO
    "\u2066"  # LRI
    "\u2067"  # RLI
    "\u2068"  # FSI
    "\u2069"  # PDI
)

_BIDI_TRANSLATION_TABLE = {ord(ch): None for ch in _BIDI_CONTROL_CHARS}


def strip_bidi_controls(text: str) -> str:
    return (text or "").translate(_BIDI_TRANSLATION_TABLE)
