"""bananachat/eggs.py is BananaSuite's shared egg cleaner: it must give the shared answers.

``fixtures/eggs-vectors.json`` is copied unchanged from BananaSuite's ``banana-eggs/v1`` files, like
``bananachat/eggs.py`` and ``bananachat/static/banana-eggs/``."""

import json
from pathlib import Path

import pytest

from bananachat import eggs

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "eggs-vectors.json").read_text(encoding="utf-8"))


def test_the_vectors_are_for_this_format():
    assert VECTORS["spec"] == "banana-eggs/v1"
    assert eggs.VERSION == 1


@pytest.mark.parametrize("case", VECTORS["clean"], ids=[case["name"] for case in VECTORS["clean"]])
def test_clean_reads_and_saves_as_every_copy_does(case):
    assert eggs.clean(case["input"]) == case["clean"]
    expected = [tuple(error) for error in case["strict_errors"]]
    if expected:
        with pytest.raises(eggs.EggError) as raised:
            eggs.clean(case["input"], strict=True)
        assert raised.value.errors == expected
    else:
        assert eggs.clean(case["input"], strict=True) == case["clean"]


@pytest.mark.parametrize("case", VECTORS["legacy"], ids=[case["name"] for case in VECTORS["legacy"]])
def test_legacy_import(case):
    assert eggs.from_legacy(case["value"], case["columns"]) == case["result"]


def test_the_default_is_the_konami_code_and_survives_cleaning():
    default = eggs.default_config()
    assert [egg["keys"] for egg in default["eggs"]] == [eggs.KONAMI]
    assert eggs.clean(eggs.dumps(default), strict=True) == default


def test_script_json_cannot_end_the_script_element():
    text = eggs.script_json({"message": "</script><script>alert(1)</script> &  "})
    assert "<" not in text and ">" not in text and "&" not in text and " " not in text
    assert json.loads(text) == {"message": "</script><script>alert(1)</script> &  "}
