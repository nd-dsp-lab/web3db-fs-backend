"""Every monkeypatch target a test names must still exist.

The e2e tests skip whenever no IPFS node is reachable, and a skip happens
before fixtures run — so a fixture patching a function that has since moved
looks exactly like a pass on a developer machine, and only fails in CI. This
checks the targets statically instead, in the unit job, with no node needed.
"""
import importlib
import os
import re

import pytest

TESTS_DIR = os.path.dirname(__file__)


def _patch_targets():
    """[(test_file, module_name, attribute)] for every monkeypatch.setattr(mod, "name")."""
    found = []
    for name in sorted(os.listdir(TESTS_DIR)):
        if not (name.startswith("test") and name.endswith(".py")):
            continue
        src = open(os.path.join(TESTS_DIR, name)).read()
        # local name -> real module, for both `import x` and `import x as y`
        aliases = {a: m for m, a in re.findall(r"^import ([\w.]+) as (\w+)$", src, re.M)}
        aliases.update({m: m for m in re.findall(r"^import ([\w.]+)$", src, re.M)})
        for obj, attr in re.findall(r'monkeypatch\.setattr\(\s*(\w+),\s*"(\w+)"', src):
            if obj in aliases:
                found.append((name, aliases[obj], attr))
    return found


def test_the_scan_finds_patches():
    # A regex that quietly matched nothing would make the check below vacuous.
    assert len(_patch_targets()) > 10


@pytest.mark.parametrize("test_file,module_name,attr", _patch_targets())
def test_patch_target_exists(test_file, module_name, attr):
    module = importlib.import_module(module_name)
    assert hasattr(module, attr), (
        f"{test_file} patches {module_name}.{attr}, which no longer exists — "
        f"the test would error at setup"
    )
