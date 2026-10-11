"""Parsing of numbers that arrive as text."""

from __future__ import annotations

# Python refuses to convert longer digit strings to int.
_CONVERSION_LIMIT = 4300


def ascii_int(value: object, max_digits: int = _CONVERSION_LIMIT) -> int | None:
    """A non-negative integer written with ASCII digits only, else ``None``.

    ``str.isdigit()`` is not enough before ``int()``: it accepts characters such
    as superscripts that ``int()`` refuses.
    """
    text = str(value)
    if text.isascii() and text.isdecimal() and len(text) <= min(max_digits, _CONVERSION_LIMIT):
        return int(text)
    return None
