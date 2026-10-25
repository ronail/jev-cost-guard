import importlib.util
import sys
from pathlib import Path

# Native plugin package root == the dir holding plugin.yaml. The host loads
# this package's `__init__.py`; under pytest we load that SAME file as a named
# module so tests can `from jev_skill_router import Router, _fm`.
_ROOT = Path(__file__).resolve().parent
_NAME = "jev_skill_router"
if _NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(_NAME, _ROOT / "__init__.py")
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = mod
    spec.loader.exec_module(mod)
sys.path.insert(0, str(_ROOT))
