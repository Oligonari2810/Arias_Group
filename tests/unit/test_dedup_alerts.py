"""Tests for app.dedup_alerts — preserve-order de-duplication of alert strings.

Real impl (app.py:657-665) uses a set for membership and preserves first-seen
order. Dedup is exact-match (case-sensitive).
"""
from app import dedup_alerts


def test_empty_lines_returns_empty_list():
    assert dedup_alerts([]) == []


def test_lines_without_alerts_key_are_safe():
    assert dedup_alerts([{}]) == []


def test_lines_with_none_alerts_are_safe():
    assert dedup_alerts([{'alerts': None}]) == []


def test_duplicate_alerts_are_collapsed():
    lines = [
        {'alerts': ['PLACAS BA13: falta precio unitario']},
        {'alerts': ['PLACAS BA13: falta precio unitario']},
        {'alerts': ['PLACAS BA13: falta precio unitario']},
    ]
    assert dedup_alerts(lines) == ['PLACAS BA13: falta precio unitario']


def test_first_appearance_order_is_preserved():
    lines = [
        {'alerts': ['C']},
        {'alerts': ['A', 'B']},
        {'alerts': ['A', 'C', 'D']},
    ]
    assert dedup_alerts(lines) == ['C', 'A', 'B', 'D']


def test_dedup_is_case_sensitive():
    lines = [{'alerts': ['Alerta X', 'alerta x', 'ALERTA X']}]
    # All three strings differ → all three kept.
    assert dedup_alerts(lines) == ['Alerta X', 'alerta x', 'ALERTA X']


def test_many_lines_mixed_alerts():
    lines = [
        {'alerts': ['a', 'b']},
        {'alerts': []},
        {'alerts': ['c']},
        {'alerts': ['b', 'd']},
    ]
    assert dedup_alerts(lines) == ['a', 'b', 'c', 'd']
