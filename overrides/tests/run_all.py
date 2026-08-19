"""Authoritative CI harness (item 10). Exit non-zero on any failure."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import test_suite  # noqa: F401
from tests import test_pipeline  # noqa: F401
from tests import test_service  # noqa: F401
from tests import test_json_persistence  # noqa: F401
from tests.framework import run

if __name__ == "__main__":
    passed, failures = run()
    print(("PASS" if not failures else "FAIL") + f" — {passed} passed, {len(failures)} failed")
    sys.exit(1 if failures else 0)
