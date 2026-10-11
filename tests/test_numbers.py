import pytest

from bananachat.numbers import ascii_int

SUPERSCRIPT_TWO = "²"
ARABIC_THREE = "٣"


@pytest.mark.parametrize("text, expected", [("0", 0), ("42", 42), ("007", 7), ("9" * 25, 10**25 - 1)])
def test_ascii_digits_are_parsed(text, expected):
    assert ascii_int(text) == expected


@pytest.mark.parametrize("text", ["", "-1", "1.5", " 1", "1e3", SUPERSCRIPT_TWO, ARABIC_THREE, "9" * 5000, None])
def test_anything_else_is_refused(text):
    assert ascii_int(text) is None


def test_the_digit_limit_can_be_lowered():
    assert ascii_int("99999", 5) == 99999
    assert ascii_int("100000", 5) is None
