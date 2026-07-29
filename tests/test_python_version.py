"""The Python version is written down in three places and they have to agree.

Renovate keeps two of them in step: the Dockerfile through its own manager, and
DEFAULT_PYTHON in the workflows through a custom manager, grouped into one pull
request. It cannot do the third. `ruff.toml` writes the version as `py314`, which
is neither a version Renovate can parse nor one that could be compared against
Docker tags, so there is nothing sensible to hand it.

So this fails instead. When Renovate opens the PR that moves Python to 3.15, this
test goes red until ruff.toml follows, which is the whole point: ruff's
target-version decides what the formatter is allowed to emit, and at py314 that
already includes syntax 3.13 cannot parse.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))


def search(path, pattern):
    match = re.search(pattern, path.read_text())
    assert match, f"{pattern} not found in {path.name}"
    return match


def dockerfile_version():
    match = search(REPO_ROOT / "Dockerfile", r"FROM python:(\d+)\.(\d+)\.(\d+)")
    return tuple(int(part) for part in match.groups())


def ruff_target():
    match = search(REPO_ROOT / "ruff.toml", r'target-version\s*=\s*"py(\d)(\d+)"')
    return tuple(int(part) for part in match.groups())


def workflow_versions():
    return {
        path.name: tuple(
            int(part)
            for part in search(path, r"DEFAULT_PYTHON:\s*(\d+)\.(\d+)\.(\d+)").groups()
        )
        for path in WORKFLOWS
        if "DEFAULT_PYTHON" in path.read_text()
    }


def test_at_least_one_workflow_pins_a_python_version():
    # Otherwise the comparison below passes by having nothing to compare.
    assert workflow_versions()


def test_the_workflows_build_on_the_same_python_as_the_image():
    for name, version in workflow_versions().items():
        assert version == dockerfile_version(), (
            f"{name} tests on {version} but the image ships {dockerfile_version()}"
        )


def test_ruff_targets_the_python_the_image_ships():
    assert ruff_target() == dockerfile_version()[:2], (
        f"ruff.toml targets py{''.join(str(p) for p in ruff_target())} but the image "
        f"ships {dockerfile_version()}. Renovate cannot rewrite ruff.toml, so it has "
        "to be updated by hand alongside the grouped python PR."
    )


def test_the_regex_renovate_uses_matches_the_workflows():
    # Kept identical to matchStrings in .github/renovate.json. If this stops
    # matching, Renovate silently stops updating DEFAULT_PYTHON at all.
    renovate_pattern = r"DEFAULT_PYTHON:\s*(?P<currentValue>\d+\.\d+\.\d+)"

    for path in WORKFLOWS:
        text = path.read_text()
        if "DEFAULT_PYTHON" in text:
            assert re.search(renovate_pattern, text), path.name
