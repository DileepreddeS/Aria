"""The resume eval: every posting through the deterministic pipeline, measured (RESUME_ENGINE §11).

Per posting: read it (Stage 1, rules), normalize (2), match (4), plan (5a), make the
fallback page in the facts' own words, print it with fit-to-page, and run 6a, 6b, 6c and 6d.

**Gates** (a run fails if any does): no truth violation (6b), no banned word (6c), every page
one page after fitting (6d, which also refuses a substituted font), no ATS read-back finding
(6a, the evidenced must-haves' spellings included), and, on the synthetic set, exact
agreement with each posting's labels. A failing label check is never fixed by editing the
label (DECISIONS 2026-10-19): the labels and the parser share an author, so the check tests
consistency, and only while the labels stay as they were written.

**Reported, not gated:** must-have coverage, fit, role type, the other recruiter findings
(the fallback page's opening verbs are the facts' own until the writer exists; step 9's
done-when list turns them into a gate), how many rounds fitting took, what was dropped.

The gates run on three synthetic postings: they catch breakage, not subtle regression. The
real postings (``evals/resume/jds``) are reported, never gated, until step 12.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from aria_resume.compose.fallback import fallback_document
from aria_resume.compose.fit import fit_to_page
from aria_resume.compose.line_budget import Page
from aria_resume.compose.planner import plan
from aria_resume.compose.templates import load_template
from aria_resume.jd.reader import read_jd
from aria_resume.match.embeddings import Embedder, WordHashEmbedder
from aria_resume.match.matcher import match
from aria_resume.normalize.requirements import DEGREE_ORDER, normalize
from aria_resume.render.html import TEMPLATE_VERSION, render_html
from aria_resume.render.links import add_links
from aria_resume.render.pdf import ChromiumPrinter
from aria_resume.schemas import EvidenceMap, NormalizedJD, ParsedJD, RequirementKind, ResumePlan, RoleType
from aria_resume.validate.ats import check_ats
from aria_resume.validate.implied import load_implied_skills
from aria_resume.validate.layout import check_layout
from aria_resume.validate.recruiter import check_recruiter
from aria_resume.validate.style import load_style
from aria_resume.validate.truth import check_truth

from aria_core.schemas.snapshot import CandidateSnapshot
from aria_core.taxonomy.loader import load_default_taxonomy
from aria_core.taxonomy.model import SkillTaxonomy

__all__ = ["BASELINES", "REPORTS", "SUITES", "Report", "baseline_view", "label_mismatches", "run_suite"]

HERE = Path(__file__).parent
SUITES = {"resume-synthetic": HERE / "synthetic", "resume": HERE / "jds"}
#: Only the synthetic suite has a baseline: it reflects the CI configuration (the fake embedder).
BASELINES = {"resume-synthetic": HERE / "baseline" / "resume-synthetic.json"}
REPORTS = HERE / "reports"
CANDIDATE = HERE / "candidate.yaml"
TODAY = dt.date(2026, 10, 19)
NOTE = (
    "The gates run on three synthetic postings: they catch breakage, not subtle regression. "
    "Label agreement tests the parser's consistency with labels written by the same author."
)

_SKILL_KINDS = (RequirementKind.SKILL, RequirementKind.DOMAIN, RequirementKind.SOFT)


def load_candidate(path: Path = CANDIDATE) -> CandidateSnapshot:
    return CandidateSnapshot.model_validate_json(
        json.dumps(yaml.safe_load(path.read_text(encoding="utf-8")), default=str)
    )


def _groups(normalized: NormalizedJD) -> set[frozenset[str]]:
    groups: dict[str, set[str]] = {}
    for requirement in normalized.requirements:
        if requirement.kind in _SKILL_KINDS and requirement.must_have:
            groups.setdefault(requirement.any_of or f"alone:{requirement.canonical}", set()).add(
                requirement.canonical
            )
    return {frozenset(members) for members in groups.values()}


def label_mismatches(
    parsed: ParsedJD, normalized: NormalizedJD, evidence: EvidenceMap, labels: dict[str, Any]
) -> list[str]:
    """Each way the pipeline's reading differs from the posting's labels, said plainly."""
    levels = [r.canonical for r in normalized.requirements if r.kind is RequirementKind.EDUCATION]
    read: dict[str, Any] = {
        "role_type": evidence.role_type.value,
        "min_years": parsed.min_years,
        "education": min(levels, key=DEGREE_ORDER.index) if levels else None,
        "work_mode": parsed.work_mode.value,
        "sponsorship": parsed.sponsorship_language is not None,
        "citizenship": parsed.clearance_or_citizenship is not None,
        "suspicious": sorted(entry.split(":", 1)[0] for entry in parsed.suspicious_instructions),
        "must_have": _groups(normalized),
        "skills": {r.canonical for r in normalized.requirements if r.kind in _SKILL_KINDS},
    }
    wanted = dict(labels)
    wanted["suspicious"] = sorted(labels.get("suspicious") or [])
    wanted["must_have"] = {frozenset(item.split("|")) for item in labels.get("must_have") or []}
    wanted["skills"] = set(labels.get("skills") or [])
    found = []
    for key in (
        "role_type",
        "min_years",
        "education",
        "work_mode",
        "sponsorship",
        "citizenship",
        "suspicious",
    ):
        if read[key] != wanted.get(key):
            found.append(f"{key}: read {read[key]!r}, labelled {wanted.get(key)!r}")
    if read["must_have"] != wanted["must_have"]:
        extra = sorted("|".join(sorted(group)) for group in read["must_have"] - wanted["must_have"])
        absent = sorted("|".join(sorted(group)) for group in wanted["must_have"] - read["must_have"])
        found.append(f"must_have: read but not labelled {extra}; labelled but not read {absent}")
    if read["skills"] != wanted["skills"]:
        found.append(
            f"skills: read but not labelled {sorted(read['skills'] - wanted['skills'])}; "
            f"labelled but not read {sorted(wanted['skills'] - read['skills'])}"
        )
    return found


@dataclass
class PostingResult:
    name: str
    gates: dict[str, Any]
    reported: dict[str, Any]
    failures: list[str] = field(default_factory=list)


@dataclass
class Report:
    suite: str
    configuration: dict[str, str]
    postings: list[PostingResult]

    @property
    def failures(self) -> list[str]:
        return [f"{posting.name}: {failure}" for posting in self.postings for failure in posting.failures]

    def as_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "note": NOTE,
            "configuration": self.configuration,
            "postings": [
                {"name": p.name, "gates": p.gates, "reported": p.reported, "failures": p.failures}
                for p in self.postings
            ],
            "passed": not self.failures,
        }


#: The reported numbers the baseline holds: decided before anything is printed, so the same on
#: every platform. Fitting rounds, bullets and dropped facts follow Chromium's layout, and the
#: recruiter findings are counted on the fitted page, so they stay in the report only.
BASELINE_REPORTED = ("role_type", "fit", "coverage")


def _baseline_value(name: str, value: Any) -> Any:
    # Coverage to 3 places: a float's last bits can differ between numpy and BLAS builds on
    # Windows and Linux, even with the same fake embedder.
    return round(value, 3) if name == "coverage" else value


def baseline_view(report: dict[str, Any]) -> dict[str, Any]:
    """What the baseline holds: decisions and counts, nothing measured from the print.

    A gate is kept as its count (or yes/no), never its findings' text, which can carry
    measured points; the label mismatches come from the parser alone and are kept whole.
    A baseline written on Windows then compares with a CI run on Linux.
    """
    return {
        "suite": report["suite"],
        "note": report["note"],
        "configuration": report["configuration"],
        "passed": report["passed"],
        "postings": [
            {
                "name": posting["name"],
                "gates": {
                    name: value if isinstance(value, bool) or name == "label_mismatches" else len(value)
                    for name, value in posting["gates"].items()
                },
                "reported": {
                    name: _baseline_value(name, posting["reported"][name]) for name in BASELINE_REPORTED
                },
            }
            for posting in report["postings"]
        ],
    }


def _run_posting(
    path: Path,
    candidate: CandidateSnapshot,
    taxonomy: SkillTaxonomy,
    embedder: Embedder,
    labels: dict[str, Any] | None,
) -> PostingResult:
    parsed = read_jd(path.read_text(encoding="utf-8"), taxonomy)
    normalized = normalize(parsed, taxonomy)
    evidence = match(normalized, candidate, taxonomy, embedder)
    template = load_template(evidence.role_type)

    def build(page: Page) -> tuple[ResumePlan, bytes]:
        result = plan(normalized, evidence, candidate, taxonomy, template=template, page=page)
        document = fallback_document(result, candidate)
        return result, add_links(asyncio.run(ChromiumPrinter().print_pdf(render_html(document))), document)

    fitted = fit_to_page(build)
    document = fallback_document(fitted.plan, candidate)
    style = load_style()
    truth = check_truth(document, candidate, taxonomy)
    recruiter = check_recruiter(document, candidate, style)
    layout = check_layout(fitted.pdf, document)
    strong_must = [
        r.surface_forms[0]
        for r in normalized.requirements
        if r.must_have and r.kind in _SKILL_KINDS and r.id in evidence.strong
    ]
    ats = check_ats(fitted.pdf, document, taxonomy, today=TODAY, keywords=strong_must)
    banned = [finding for finding in recruiter if finding.check == "banned_word"]
    gates: dict[str, Any] = {
        "truth_violations": [finding.detail for finding in truth],
        "banned_words": [finding.detail for finding in banned],
        "one_page": fitted.fits and not any(finding.check == "one_page" for finding in layout),
        "layout_findings": [finding.detail for finding in layout],
        "ats_findings": [finding.detail for finding in ats],
    }
    result = PostingResult(
        name=path.stem,
        gates=gates,
        reported={
            "role_type": evidence.role_type.value,
            "fit": evidence.fit_decision.value,
            "coverage": evidence.coverage,
            "recruiter_findings": dict(
                sorted(Counter(f.check for f in recruiter if f.check != "banned_word").items())
            ),
            "fit_rounds": fitted.rounds,
            "dropped_facts": len(fitted.plan.dropped),
            "bullets": sum(len(entry.bullets) for section in document.sections for entry in section.entries),
        },
    )
    for name in ("truth_violations", "banned_words", "layout_findings", "ats_findings"):
        if gates[name]:
            result.failures.append(f"{name}: {gates[name]}")
    if not gates["one_page"]:
        result.failures.append("one_page: the page did not fit after fitting")
    if labels is not None:
        mismatches = label_mismatches(parsed, normalized, evidence, labels)
        gates["label_mismatches"] = mismatches
        result.failures.extend(f"labels: {item}" for item in mismatches)
    return result


def run_suite(name: str, *, embedder: Embedder | None = None) -> Report:
    directory = SUITES[name]
    taxonomy = load_default_taxonomy()
    embedder = embedder or WordHashEmbedder()
    candidate = load_candidate()
    postings = []
    for path in sorted(directory.rglob("*.txt")):
        label_path = path.with_name(f"{path.stem}.labels.yaml")
        gated_labels = (
            yaml.safe_load(label_path.read_text(encoding="utf-8")) if name == "resume-synthetic" else None
        )
        postings.append(_run_posting(path, candidate, taxonomy, embedder, gated_labels))
    configuration = {
        "embedder": embedder.model_id,
        "taxonomy": taxonomy.version,
        "style": load_style().version,
        "implied_skills": load_implied_skills(taxonomy).version,
        # The role templates this run used, each at its version.
        "templates": ", ".join(
            sorted(
                {
                    f"{p.reported['role_type']}@{load_template(RoleType(p.reported['role_type'])).version}"
                    for p in postings
                }
            )
        ),
        "render_template": TEMPLATE_VERSION,
    }
    return Report(name, configuration, postings)
