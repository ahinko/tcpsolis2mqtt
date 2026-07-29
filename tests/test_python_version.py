"""The Python version is declared once, in the Dockerfile, and repeated once more
in ruff.toml where it cannot be avoided.

It used to be declared three times, the third being DEFAULT_PYTHON in the
workflows. That made two Renovate dependencies with different constraints: the
Dockerfile was pinned to an alpine line it could not leave, a bare version had no
such limit, and PR #158 duly moved one without the other. CI now reads the version
out of the Dockerfile instead, so there is one source and nothing to synchronise.

ruff.toml is the exception. It writes the version as `py314`, which is neither a
version Renovate can parse nor one it could compare against Docker tags, so there
is nothing sensible to hand it. This fails instead, which turns the Python PR red
until ruff.toml follows. That matters more than it looks: target-version decides
what the formatter may emit, and at py314 that already includes syntax 3.13 cannot
parse.
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


def test_ruff_targets_the_python_the_image_ships():
    assert ruff_target() == dockerfile_version()[:2], (
        f"ruff.toml targets py{''.join(str(p) for p in ruff_target())} but the image "
        f"ships {dockerfile_version()}. Renovate cannot rewrite ruff.toml, so it has "
        "to be updated by hand alongside the Python PR."
    )


def test_no_workflow_declares_its_own_python_version():
    # The whole point of reading it from the Dockerfile. A second declaration is a
    # second Renovate dependency, and the two drift.
    for path in WORKFLOWS:
        text = path.read_text()

        assert "DEFAULT_PYTHON" not in text, (
            f"{path.name} declares a Python version of its own. Read it from the "
            "Dockerfile instead, see the step in pull_request.yaml."
        )
        assert not re.search(r"python-version:\s*['\"]?\d+\.\d+", text), (
            f"{path.name} hardcodes a python-version"
        )


def test_the_workflow_reads_the_version_the_way_this_test_does():
    # Kept identical to the sed in pull_request.yaml. If the Dockerfile FROM line
    # changes shape, CI would silently set up no Python at all rather than fail.
    workflow = (REPO_ROOT / ".github" / "workflows" / "pull_request.yaml").read_text()
    assert "FROM python:" in workflow, "the extraction step is gone"

    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    extracted = re.search(r"^FROM python:([0-9][0-9.]*)-", dockerfile, re.MULTILINE)

    assert extracted, "the sed in the workflow would produce an empty version"
    assert tuple(int(p) for p in extracted.group(1).split(".")) == dockerfile_version()
