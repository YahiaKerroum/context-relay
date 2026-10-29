"""One throwaway home folder shared by every test module in a run."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if "CONTEXT_RELAY_TEST_TMP" not in os.environ:
    os.environ["CONTEXT_RELAY_TEST_TMP"] = tempfile.mkdtemp(prefix="relay-test-")
TMP = Path(os.environ["CONTEXT_RELAY_TEST_TMP"])
HOME = TMP / "home"
os.environ["CONTEXT_RELAY_HOME"] = str(HOME)
# the package is importable here, and in the hook subprocesses the tests start
sys.path.insert(0, str(SRC))
os.environ["PYTHONPATH"] = str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")
