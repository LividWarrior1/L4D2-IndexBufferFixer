"""
CI entry point: run the suite and turn every failure into a GitHub
annotation (::error ...), so failures are readable from the PR page and the
checks API without downloading logs.
"""
import sys
import unittest
from pathlib import Path


def esc(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def main() -> int:
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here.parent))
    suite = unittest.defaultTestLoader.discover(str(here), top_level_dir=str(here))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    for test, tb in result.failures + result.errors:
        print(f"::error title={esc(test.id())}::{esc(tb[-3500:])}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
