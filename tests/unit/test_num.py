"""Tests for app._num — tolerant numeric coercion helper.

The real implementation (app.py:508-512) is essentially:
    float(v) if v is not None else 0.0, catching TypeError/ValueError → 0.0.
So "1,234.56" returns 0.0 (commas not handled) and "" returns 0.0.
"""
from app import _num


def test_num_none_returns_zero():
    assert _num(None) == 0.0


def test_num_zero_int():
    assert _num(0) == 0.0


def test_num_positive_float_passthrough():
    assert _num(1.5) == 1.5


def test_num_numeric_string():
    assert _num("2.5") == 2.5


def test_num_non_numeric_string_returns_zero():
    assert _num("abc") == 0.0


def test_num_empty_string_returns_zero():
    assert _num("") == 0.0


def test_num_list_returns_zero():
    assert _num([]) == 0.0


def test_num_dict_returns_zero():
    assert _num({'x': 1}) == 0.0


def test_num_negative_string():
    assert _num("-7.25") == -7.25


def test_num_integer_string():
    assert _num("42") == 42.0


def test_num_comma_decimal_not_supported():
    # European-style "2,5" is not parseable by float() → coerced to 0.0.
    # Documents current behaviour; not a bug per SPEC-001 scope.
    assert _num("2,5") == 0.0


def test_num_returns_float_type():
    assert isinstance(_num(3), float)
    assert isinstance(_num("5"), float)
    assert isinstance(_num(None), float)
