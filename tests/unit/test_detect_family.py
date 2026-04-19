"""Tests for app.detect_family — category → family mapping.

Real FAMILY_MAP (app.py:492-505) maps lower-cased, stripped category strings
to one of: PLACAS, PERFILES, TORNILLOS, CINTAS, ACCESORIOS, PASTAS, TRAMPILLAS,
GYPSOCOMETE. Anything else → 'DESCONOCIDA'.
"""
import pytest

from app import detect_family


@pytest.mark.parametrize("category,expected", [
    ('placas',   'PLACAS'),
    ('placa',    'PLACAS'),
    ('placa yeso', 'PLACAS'),
    ('PLACAS',   'PLACAS'),
    ('  Placas  ', 'PLACAS'),
])
def test_detect_family_placas_variants(category, expected):
    assert detect_family(category) == expected


@pytest.mark.parametrize("category,expected", [
    ('perfiles', 'PERFILES'),
    ('perfil',   'PERFILES'),
    ('Perfiles', 'PERFILES'),
])
def test_detect_family_perfiles_variants(category, expected):
    assert detect_family(category) == expected


def test_detect_family_tornillos():
    assert detect_family('tornillos') == 'TORNILLOS'


@pytest.mark.parametrize("category", ['cintas', 'cinta', 'mallas', 'malla'])
def test_detect_family_cintas_and_mallas(category):
    assert detect_family(category) == 'CINTAS'


@pytest.mark.parametrize("category", ['accesorios', 'accesorio'])
def test_detect_family_accesorios(category):
    assert detect_family(category) == 'ACCESORIOS'


@pytest.mark.parametrize("category", [
    'pastas', 'pasta', 'adhesivo', 'adhesivos',
    'revoco', 'revocos', 'mampostería', 'mamposteria',
])
def test_detect_family_pastas_and_aliases(category):
    assert detect_family(category) == 'PASTAS'


def test_detect_family_impermeabilizacion_with_and_without_accent():
    # FAMILY_MAP explicitly lists both spellings.
    assert detect_family('impermeabilización') == 'PASTAS'
    assert detect_family('impermeabilizacion') == 'PASTAS'


@pytest.mark.parametrize("category", ['trampillas', 'trampilla'])
def test_detect_family_trampillas(category):
    assert detect_family(category) == 'TRAMPILLAS'


def test_detect_family_gypsocomete():
    assert detect_family('gypsocomete') == 'GYPSOCOMETE'


def test_detect_family_none_returns_desconocida():
    assert detect_family(None) == 'DESCONOCIDA'


def test_detect_family_empty_string_returns_desconocida():
    assert detect_family('') == 'DESCONOCIDA'


def test_detect_family_whitespace_only_returns_desconocida():
    assert detect_family('   ') == 'DESCONOCIDA'


def test_detect_family_unknown_category_returns_desconocida():
    assert detect_family('categoría_rara') == 'DESCONOCIDA'


def test_detect_family_is_not_substring_match():
    # Real implementation uses dict.get on the full normalised string, not
    # a substring search. "PLACAS EXTRA" is not a key, so it's DESCONOCIDA.
    assert detect_family('placas extra') == 'DESCONOCIDA'
