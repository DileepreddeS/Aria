"""style.yaml: the recruiter check's voice rules, versioned and checked when loaded."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from aria_resume.validate.style import STYLE_PATH, StyleError, load_style

#: RESUME_ENGINE §1 rule 5, as the spec lists them. The shipped list may grow; it may not drop one.
SPEC_BANNED = (
    "leveraged spearheaded cutting-edge synergy robust seamlessly utilize facilitate innovative "
    "streamlined orchestrated championed transformative impactful holistic paradigm dynamic "
    "proactive revolutionized best-in-class world-class game-changing passionate results-driven "
    "detail-oriented"
).split()

#: §1 rule 4's default voice.
SPEC_VERBS = (
    "built",
    "shipped",
    "wired",
    "added",
    "trained",
    "led",
    "set up",
    "sped up",
    "improved",
    "containerized",
    "designed",
    "cut",
    "migrated",
    "automated",
)


def test_the_shipped_style_loads_with_a_version() -> None:
    style = load_style()
    assert style.version
    assert style.max_bullets_per_verb == 3
    assert style.min_quantified_share == 0.6


def test_every_word_the_spec_bans_is_banned() -> None:
    assert set(SPEC_BANNED) <= set(load_style().banned)


def test_the_default_verbs_are_the_spec_s() -> None:
    assert set(load_style().allowed_verbs) == set(SPEC_VERBS)


def test_the_spec_s_weak_openings_and_vague_phrases_are_there() -> None:
    style = load_style()
    assert {"responsible for", "worked on", "helped"} <= set(style.weak_openings)
    assert {"cloud-native solutions", "modern web systems", "digital solutions"} <= set(style.vague_phrases)


def _write(tmp_path: Path, **changes: object) -> Path:
    data = yaml.safe_load(STYLE_PATH.read_text(encoding="utf-8"))
    data.update(changes)
    path = tmp_path / "style.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        ({"allowed_verbs": ["Built"]}, "lowercase"),
        ({"allowed_verbs": ["built", "built"]}, "twice"),
        ({"allowed_verbs": ["built", "leveraged"]}, "banned"),
        ({"weak_openings": ["helped", "built"]}, "allowed verb"),
        ({"max_bullets_per_verb": 0}, "max_bullets_per_verb"),
        ({"min_quantified_share": 1.5}, "min_quantified_share"),
        ({"version": ""}, "version"),
        ({"surprise": ["x"]}, "surprise"),
    ],
)
def test_a_broken_style_is_refused(tmp_path: Path, changes: dict[str, object], says: str) -> None:
    with pytest.raises(StyleError, match=says):
        load_style(_write(tmp_path, **changes))


def test_a_missing_list_is_refused(tmp_path: Path) -> None:
    data = yaml.safe_load(STYLE_PATH.read_text(encoding="utf-8"))
    del data["banned"]
    path = tmp_path / "style.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(StyleError, match="banned"):
        load_style(path)
