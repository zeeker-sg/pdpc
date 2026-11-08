"""Every module under resources/ must import cleanly.

zeeker loads each resource by file path. A dependency bump that removes or renames
something the modules import at top level (httpx, tenacity, bs4, lxml, click via
zeeker, sqlite_utils via zeeker, zeeker.Skip) fails here before any sync runs.
"""

import importlib

import pytest
from helpers import RESOURCES_DIR

MODULES = sorted(p.stem for p in RESOURCES_DIR.glob("*.py") if p.stem != "__init__")


def test_resources_discovered():
    assert {
        "_backfill",
        "enforcement_decisions",
        "enforcement_decisions_fragments",
        "guidance_by_topic",
        "regulatory_guidance",
    } <= set(MODULES)


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    module = importlib.import_module(name)
    assert module.__file__.endswith(f"{name}.py")


@pytest.mark.parametrize(
    "name",
    [
        "enforcement_decisions",
        "enforcement_decisions_fragments",
        "guidance_by_topic",
        "regulatory_guidance",
    ],
)
def test_zeeker_entry_points(name):
    """The hooks zeeker >= 0.9.0 looks up on a resource module."""
    module = importlib.import_module(name)
    assert callable(module.fetch_data)


@pytest.mark.parametrize(
    "name", ["enforcement_decisions", "guidance_by_topic", "regulatory_guidance"]
)
def test_fragment_hooks_present(name):
    module = importlib.import_module(name)
    for hook in ("transform_data", "migrate_schema", "fetch_fragments_data"):
        assert callable(getattr(module, hook)), hook


def test_zeeker_skip_api():
    from zeeker import Skip

    exc = Skip("nope", kind="blocked")
    assert (exc.reason, exc.kind) == ("nope", "blocked")
    with pytest.raises(ValueError):
        Skip("bad", kind="not-a-kind")
