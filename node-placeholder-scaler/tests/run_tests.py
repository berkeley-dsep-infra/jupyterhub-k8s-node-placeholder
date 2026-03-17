"""
Run all tests in the tests directory.

Execute from node-placeholder-scaler/:
    python tests/run_tests.py
"""

import sys
from pathlib import Path

import pytest

# Ensure the project root (node-placeholder-scaler/) is on sys.path so that
# the 'scaler' package can be imported by the test modules.
sys.path.insert(0, str(Path(__file__).parent.parent))

if __name__ == "__main__":
    tests_dir = Path(__file__).resolve().parent
    print(f"Running tests in {tests_dir}...")
    result = pytest.main(
        [
            str(tests_dir / "test_scaler.py"),
            str(tests_dir / "test_calendar_parser.py"),
            "-v",
        ]
    )
    sys.exit(result)
