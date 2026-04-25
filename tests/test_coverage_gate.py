"""Per-function coverage gate for SPEC-001 §6.4.

Reads `coverage.json` written by pytest-cov at session-end.

Because pytest-cov writes the JSON only after all tests finish, this file must
be invoked AFTER a first `pytest` run:

    pytest                                          # writes coverage.json
    pytest tests/test_coverage_gate.py --no-cov     # validates the gate

Running it as part of the main pytest invocation produces a skip on a clean
checkout (expected behaviour); the CI job runs both commands in sequence.
"""
import json
import pathlib

import pytest


TARGETS = {
    '_num':                 0.85,
    'detect_family':        0.85,
    'compute_line':         0.85,
    '_container_result':    0.85,
    'estimate_containers':  0.85,
    'compute_totals':       0.85,
    'dedup_alerts':         0.85,
    'calculate_quote':      0.90,   # orquestador crítico
}

# Line ranges (inclusive) of each function in app.py. **Must be kept in sync**
# whenever these functions are edited or code is inserted above them — otherwise
# the gate measures the wrong lines and either fails spuriously or passes blindly.
FUNCTION_LINE_RANGES = {
    '_num':                 (2015, 2019),
    'detect_family':        (2022, 2023),
    'compute_line':         (2026, 2082),
    '_container_result':    (2085, 2101),
    'estimate_containers':  (2104, 2138),
    'compute_totals':       (2261, 2281),
    'dedup_alerts':         (2284, 2292),
    'calculate_quote':      (2393, 2479),
}


def _load_coverage_json():
    cov_path = pathlib.Path('coverage.json')
    if not cov_path.exists():
        pytest.skip(
            'coverage.json no existe — ejecuta `pytest` una vez antes de invocar el gate'
        )
    data = json.loads(cov_path.read_text())
    files = data.get('files', {})
    entry = files.get('app.py') or files.get('./app.py')
    if entry is None:
        pytest.fail(f'No hay cobertura para app.py en coverage.json (keys: {list(files)[:5]})')
    executed = set(entry.get('executed_lines', []))
    missing = set(entry.get('missing_lines', []))
    return executed, missing


def test_coverage_gate_per_function():
    executed, missing = _load_coverage_json()
    executable = executed | missing

    failures = []
    report = []
    for fn, threshold in TARGETS.items():
        start, end = FUNCTION_LINE_RANGES[fn]
        fn_range = set(range(start, end + 1))
        fn_executable = fn_range & executable
        if not fn_executable:
            failures.append(f'{fn}: no executable lines detected in {start}-{end}')
            continue
        covered = fn_range & executed
        pct = len(covered) / len(fn_executable)
        report.append(
            f'  {fn:<22} {pct:>6.1%}  ({len(covered)}/{len(fn_executable)} executable, threshold {threshold:.0%})'
        )
        if pct < threshold:
            failures.append(f'{fn}: {pct:.1%} < {threshold:.0%}')

    print('\nCoverage gate report:\n' + '\n'.join(report))
    assert not failures, 'Coverage gate failures:\n  ' + '\n  '.join(failures)
