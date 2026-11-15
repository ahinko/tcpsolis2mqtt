"""The Python version is declared once, in the Dockerfile, and repeated once more
in ruff.toml where it cannot be avoided.

It used to be declared three times, the third being DEFAULT_PYTHON in the
workflows. That made two Renovate dependencies with different constraints: the
Dockerfile was pinned to an alpine line it could not leave, a bare version had no
such limit, and PR #158 duly moved one without the other. CI now reads the version
out of the Dockerfile instead, so there is one source and nothing to synchronise.

The Dockerfile pins the alpine line as well (`-alpine3.24`), and Renovate normally
treats such a suffix as a constraint it never crosses. That is what froze the image
at 3.14.5 once, so .github/renovate.json versions the line with a regex that reads
the alpine minor as a number. The tests below fail if the tag and that regex stop
matching, which would silently turn Renovate off for the image.

ruff.toml is the exception. It writes the version as `py315`, so a custom manager
in .github/renovate.json reads it back as `3.15`, tracks the two-part python tag,
and writes it without the dot, landing on the same branch as the Dockerfile. This
still fails if the two disagree, because target-version decides what the formatter
may emit, and at py315 that already includes syntax 3.13 cannot parse.
"""

import json
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
        f"ships {dockerfile_version()}. Renovate's custom manager for ruff.toml should "
        "move both in one PR; check .github/renovate.json."
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


def renovate_rules():
    config = json.loads((REPO_ROOT / ".github" / "renovate.json").read_text())
    return config["packageRules"], config["customManagers"]


def dockerfile_tag():
    return search(REPO_ROOT / "Dockerfile", r"(?m)^FROM python:(\S+)$").group(1)


def dockerfile_versioning():
    rules, _ = renovate_rules()
    (rule,) = [r for r in rules if "versioning" in r]
    assert rule["matchManagers"] == ["dockerfile"], (
        "the regex versioning must not reach the ruff.toml manager, whose current "
        "value is the bare two-part tag"
    )
    assert rule["matchPackageNames"] == ["python"]
    assert rule["versioning"].startswith("regex:")
    return rule["versioning"].removeprefix("regex:")


def test_the_dockerfile_pins_python_and_the_alpine_line():
    assert re.fullmatch(r"\d+\.\d+\.\d+-alpine3\.\d+", dockerfile_tag()), (
        f"{dockerfile_tag()!r} is not <major>.<minor>.<patch>-alpine3.<minor>"
    )


def test_renovate_can_read_the_dockerfile_tag():
    # Renovate's regex uses (?<name>); Python spells it (?P<name>).
    pattern = re.sub(r"\(\?<(\w+)>", r"(?P<\1>", dockerfile_versioning())
    match = re.fullmatch(pattern, dockerfile_tag())

    assert match, "renovate.json no longer matches the Dockerfile tag"
    assert {"major", "minor", "patch", "build"} <= set(match.groupdict())
    assert tuple(int(match[g]) for g in ("major", "minor", "patch")) == (
        dockerfile_version()
    )


def test_the_alpine_line_is_not_a_compatibility_constraint():
    # A `compatibility` group is what makes Renovate refuse to leave an alpine line.
    assert "compatibility" not in dockerfile_versioning()


def test_the_ruff_manager_keeps_plain_docker_versioning():
    _, managers = renovate_rules()
    (ruff,) = managers

    assert ruff["versioningTemplate"] == "docker"
