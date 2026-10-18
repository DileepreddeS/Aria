"""Stage 5a, the Planner (RESUME_ENGINE §8), its line budget and templates, and the fallback page."""

from __future__ import annotations

import datetime as dt
import io
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from aria_resume.compose.fallback import fallback_document
from aria_resume.compose.fit import fit_to_page, overflow
from aria_resume.compose.line_budget import PAGE, Page, bullet_lines, wrapped_lines
from aria_resume.compose.metrics import carlito
from aria_resume.compose.planner import PlanError, plan
from aria_resume.compose.templates import ROLES, TemplateError, load_template
from aria_resume.document import SectionKind
from aria_resume.jd.reader import read_jd
from aria_resume.match.embeddings import WordHashEmbedder
from aria_resume.match.matcher import match
from aria_resume.normalize.requirements import normalize
from aria_resume.schemas import EvidenceMap, NormalizedJD, PlannedEntry, ResumePlan, RoleType
from aria_resume.validate.truth import check_truth

from aria_core.schemas.snapshot import CandidateSnapshot, SnapshotContact, SnapshotEducation
from aria_core.taxonomy.model import SkillTaxonomy

JDS = Path(__file__).parent / "fixtures" / "jds"


class TestMetrics:
    def test_carlito_s_widths(self) -> None:
        regular, bold = carlito(), carlito("Bold")
        assert regular.units_per_em == 2048
        assert regular.width("m", 10.5) > regular.width("i", 10.5)
        assert bold.width("Built", 10.5) > regular.width("Built", 10.5)

    def test_an_unmapped_character_takes_the_notdef_width(self) -> None:
        regular = carlito()
        assert regular.width("\U0001f600", 10.5) == regular.fallback * 10.5 / regular.units_per_em


class TestWrapping:
    def test_a_short_bullet_is_one_line(self) -> None:
        assert bullet_lines("Built 15+ React screens") == 1

    def test_a_long_bullet_wraps(self) -> None:
        text = " ".join(["Shipped eight full-stack applications with Node.js and PostgreSQL"] * 4)
        assert bullet_lines(text) >= 3

    def test_a_word_wider_than_the_line_still_takes_a_line(self) -> None:
        assert wrapped_lines("x" * 400, 100.0) == 1

    def test_the_first_line_s_extra_room_is_used(self) -> None:
        text = "word " * 40
        assert wrapped_lines(text, 200.0, first_line_extra=200.0) < wrapped_lines(text, 200.0)


class TestTemplates:
    @pytest.mark.parametrize("role", list(RoleType))
    def test_every_role_has_a_template(self, role: RoleType) -> None:
        template = load_template(role)
        assert template.role is role and template.summary == "model_only"

    def test_ml_research_leads_with_education(self) -> None:
        assert load_template(RoleType.ML_RESEARCH).section_order[0] is SectionKind.EDUCATION

    @pytest.mark.parametrize(
        ("change", "says"),
        [
            ({"summary": "template"}, "model_only"),
            ({"older_role_bullets": [3, 2]}, "minimum"),
            ({"section_order": ["experience", "skills"]}, "education"),
            ({"section_order": ["summary", "experience", "skills", "education"]}, "summary"),
            ({"skills": {"categories": ["languages"], "max_terms_per_line": 10}}, "categories"),
            ({"role": "frontend"}, "declares role"),
        ],
    )
    def test_a_contradictory_template_is_refused(
        self, tmp_path: Path, change: dict[str, object], says: str
    ) -> None:
        data = yaml.safe_load((ROLES / "backend.yaml").read_text(encoding="utf-8"))
        data.update(change)
        (tmp_path / "backend.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        with pytest.raises(TemplateError, match=says):
            load_template(RoleType.BACKEND, tmp_path)


def _named(snapshot: CandidateSnapshot) -> CandidateSnapshot:
    degree = SnapshotEducation(
        education_id=uuid.UUID(int=7),
        school="Cascadia State University",
        degree="M.S.",
        field_of_study="Computer Science",
        end_month=dt.date(2026, 12, 1),
    )
    return snapshot.model_copy(
        update={
            "contact": SnapshotContact(
                full_name="Jordan A. Rivera",
                email="jordan.rivera@example.com",
                phone="(555) 014-2288",
                location_city="Portland",
                location_region="OR",
                linkedin_url="https://www.linkedin.com/in/jordan-rivera-example",
            ),
            "education": (degree,),
        }
    )


def _inputs(
    name: str, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
) -> tuple[NormalizedJD, EvidenceMap]:
    normalized = normalize(read_jd((JDS / f"{name}.txt").read_text(encoding="utf-8"), taxonomy), taxonomy)
    return normalized, match(normalized, snapshot, taxonomy, WordHashEmbedder())


def _entries(result: ResumePlan, kind: SectionKind) -> tuple[PlannedEntry, ...]:
    return next((section.entries for section in result.sections if section.kind is kind), ())


class TestTheLarkspurPlan:
    @pytest.fixture
    def candidate(self, snapshot: CandidateSnapshot) -> CandidateSnapshot:
        return _named(snapshot)

    @pytest.fixture
    def result(self, candidate: CandidateSnapshot, taxonomy: SkillTaxonomy) -> ResumePlan:
        normalized, evidence = _inputs("larkspur_backend", candidate, taxonomy)
        return plan(normalized, evidence, candidate, taxonomy)

    def test_roles_reverse_chronologically_with_the_template_s_counts(
        self, result: ResumePlan, ids: SimpleNamespace
    ) -> None:
        nordvale, brightfold = _entries(result, SectionKind.EXPERIENCE)
        assert (nordvale.source_id, brightfold.source_id) == (ids.nordvale, ids.brightfold)
        # The most recent role: at least 3 (it has three facts); older roles at least 2.
        assert len(nordvale.bullets) == 3 and len(brightfold.bullets) == 2

    def test_the_best_evidence_for_a_must_have_is_mandatory(
        self, result: ResumePlan, ids: SimpleNamespace
    ) -> None:
        slots = [slot for entry in _entries(result, SectionKind.EXPERIENCE) for slot in entry.bullets]
        (billing,) = [slot for slot in slots if slot.fact_ids == (ids.billing,)]
        assert billing.mandatory and "skill:python" in billing.requirement_ids

    def test_no_fact_twice(self, result: ResumePlan) -> None:
        facts = [
            fact
            for section in result.sections
            for entry in section.entries
            for slot in entry.bullets
            for fact in slot.fact_ids
        ]
        assert len(facts) == len(set(facts))

    def test_the_skills_lines_use_the_posting_s_spelling_and_no_sibling(self, result: ResumePlan) -> None:
        (skills,) = [section for section in result.sections if section.kind is SectionKind.SKILLS]
        text = " ".join(skills.lines)
        assert skills.lines[0].startswith("Languages: Python")
        # The posting wrote "Postgres" first; the candidate's PostgreSQL is the same skill.
        assert "Postgres" in text and result.surface_forms["postgresql"] == "Postgres"
        # SQL by the allowlist (PostgreSQL -> SQL); never MariaDB, a sibling the candidate lacks.
        assert "SQL" in text and "MariaDB" not in text and "Go" not in text.split(", ")

    def test_an_implied_skill_is_listed_only_through_the_allowlist(self, result: ResumePlan) -> None:
        (skills,) = [section for section in result.sections if section.kind is SectionKind.SKILLS]
        terms = {term for line in skills.lines for term in line.split(": ", 1)[1].split(", ")}
        # React -> JavaScript is on implied_skills.yaml; React -> HTML is a taxonomy edge that is not.
        assert "JavaScript" in terms
        assert "HTML" not in terms

    def test_it_fits_and_follows_the_template_s_order(self, result: ResumePlan) -> None:
        assert result.estimated_height <= result.usable_height
        order = load_template(RoleType.BACKEND).section_order
        kinds = [section.kind for section in result.sections]
        assert kinds == [kind for kind in order if kind in kinds]
        assert result.role_type is RoleType.BACKEND and result.dropped == ()

    def test_no_name_no_page(self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy) -> None:
        normalized, evidence = _inputs("larkspur_backend", snapshot, taxonomy)
        with pytest.raises(PlanError, match="name"):
            plan(normalized, evidence, snapshot.model_copy(update={"contact": SnapshotContact()}), taxonomy)


class TestATightPage:
    """A page too short for everything: what goes first, and what never does."""

    def _tight(self, candidate: CandidateSnapshot, taxonomy: SkillTaxonomy, height: float) -> ResumePlan:
        normalized, evidence = _inputs("larkspur_backend", candidate, taxonomy)
        return plan(normalized, evidence, candidate, taxonomy, page=Page(height=height))

    @pytest.mark.parametrize("height", [300.0, 260.0, 220.0, 190.0, 160.0])
    def test_no_role_ever_keeps_one_bullet(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, height: float
    ) -> None:
        result = self._tight(_named(snapshot), taxonomy, height)
        for entry in _entries(result, SectionKind.EXPERIENCE):
            assert len(entry.bullets) != 1
            assert entry.header_only == (len(entry.bullets) == 0)

    def test_a_role_that_cannot_hold_two_keeps_its_header(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        result = self._tight(_named(snapshot), taxonomy, 190.0)
        roles = _entries(result, SectionKind.EXPERIENCE)
        assert len(roles) == 2, "a role was dropped instead of kept as a header"
        assert any(entry.header_only for entry in roles)

    def test_cuts_are_lowest_value_first_and_mandatory_bullets_last(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, ids: SimpleNamespace
    ) -> None:
        candidate = _named(snapshot)
        roomy = self._tight(candidate, taxonomy, PAGE.height)
        values = {
            slot.fact_ids[0]: (slot.mandatory, slot.value)
            for section in roomy.sections
            for entry in section.entries
            for slot in entry.bullets
        }
        tight = self._tight(candidate, taxonomy, 250.0)
        assert tight.dropped, "the tight page dropped nothing"
        kept = {
            slot.fact_ids[0]
            for section in tight.sections
            for entry in section.entries
            for slot in entry.bullets
        }
        assert ids.billing in kept  # mandatory for Python
        for gone in tight.dropped:
            if gone in values:
                assert not values[gone][0] or all(values[fact][0] for fact in kept)

    def test_projects_go_whole(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, ids: SimpleNamespace
    ) -> None:
        # The AI posting, where the RAG project carries evidence and is on the page; shrink the
        # page until the project is cut, and it must go whole, never stay as a bare header.
        candidate = _named(snapshot)
        normalized, evidence = _inputs("corvane_ai", candidate, taxonomy)
        roomy = plan(normalized, evidence, candidate, taxonomy)
        assert [entry.source_id for entry in _entries(roomy, SectionKind.PROJECTS)] == [ids.vera]
        cut = None
        for height in range(400, 100, -10):
            result = plan(normalized, evidence, candidate, taxonomy, page=Page(height=float(height)))
            if ids.rag in result.dropped:
                cut = result
                break
        assert cut is not None, "no page height cut the project"
        assert _entries(cut, SectionKind.PROJECTS) == ()

    def test_a_degree_is_never_cut(self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy) -> None:
        result = self._tight(_named(snapshot), taxonomy, 160.0)
        assert len(_entries(result, SectionKind.EDUCATION)) == 1


class TestTheFallbackPage:
    @pytest.fixture
    def page(self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy) -> tuple[CandidateSnapshot, object]:
        candidate = _named(snapshot)
        normalized, evidence = _inputs("larkspur_backend", candidate, taxonomy)
        return candidate, fallback_document(plan(normalized, evidence, candidate, taxonomy), candidate)

    def test_it_passes_the_truth_check_headers_included(self, page: tuple, taxonomy: SkillTaxonomy) -> None:  # type: ignore[type-arg]
        candidate, document = page
        assert check_truth(document, candidate, taxonomy) == []

    def test_bullets_are_the_facts_own_words_and_there_is_no_summary(self, page: tuple) -> None:  # type: ignore[type-arg]
        candidate, document = page
        texts = {fact.text for fact in candidate.facts}
        bullets = [
            bullet.text
            for section in document.sections
            for entry in section.entries
            for bullet in entry.bullets
        ]
        assert bullets and all(text in texts for text in bullets)
        assert SectionKind.SUMMARY not in {section.kind for section in document.sections}

    def test_the_contact_line_holds_approved_details_only(self, page: tuple) -> None:  # type: ignore[type-arg]
        _, document = page
        assert document.contact_line == (
            "jordan.rivera@example.com | (555) 014-2288 | Portland, OR | "
            "linkedin.com/in/jordan-rivera-example"
        )

    def test_a_degree_prints_with_its_field(self, page: tuple) -> None:  # type: ignore[type-arg]
        _, document = page
        (education,) = [section for section in document.sections if section.kind is SectionKind.EDUCATION]
        (degree,) = education.entries
        assert (degree.title, degree.detail, degree.dates) == (
            "Cascadia State University",
            "M.S. in Computer Science",
            "Dec 2026",
        )


@pytest.mark.chromium
class TestPrinted:
    def test_the_estimate_matches_the_printed_width(self) -> None:
        import asyncio
        import io

        import pdfplumber
        from aria_resume.document import DocumentBullet, DocumentEntry, DocumentSection, ResumeDocument
        from aria_resume.render.html import render_html
        from aria_resume.render.pdf import ChromiumPrinter

        text = "Shipped eight full stack applications with Node and PostgreSQL across three teams"
        document = ResumeDocument(
            name="Jordan A. Rivera",
            sections=(
                DocumentSection(
                    kind=SectionKind.EXPERIENCE,
                    entries=(
                        DocumentEntry(title="Nordvale Consulting", bullets=(DocumentBullet(text=text),)),
                    ),
                ),
            ),
        )
        pdf = asyncio.run(ChromiumPrinter().print_pdf(render_html(document)))
        with pdfplumber.open(io.BytesIO(pdf)) as opened:
            words = opened.pages[0].extract_words()
        first, last = (
            next(w for w in words if w["text"] == "Shipped"),
            next(w for w in words if w["text"] == "teams"),
        )
        printed = last["x1"] - first["x0"]
        assert carlito().width(text, PAGE.size) == pytest.approx(printed, rel=0.03)

    def test_the_fallback_page_prints_on_one_page_and_reads_back(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        import asyncio

        from aria_resume.render.html import render_html
        from aria_resume.render.links import add_links
        from aria_resume.render.pdf import ChromiumPrinter
        from aria_resume.validate.ats import check_ats
        from aria_resume.validate.layout import check_layout

        candidate = _named(snapshot)
        normalized, evidence = _inputs("larkspur_backend", candidate, taxonomy)
        document = fallback_document(plan(normalized, evidence, candidate, taxonomy), candidate)
        pdf = add_links(asyncio.run(ChromiumPrinter().print_pdf(render_html(document))), document)
        assert check_layout(pdf, document) == []
        assert check_ats(pdf, document, taxonomy, today=dt.date(2026, 10, 18)) == []


def _pages(count: int) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(count):
        writer.add_blank_page(612, 792)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class TestFitToPage:
    """The printed page decides (RESUME_ENGINE section 8 5a: re-check after rendering)."""

    def _plan(self) -> ResumePlan:
        return ResumePlan(
            role_type=RoleType.SWE, template_version="t", sections=(), estimated_height=0, usable_height=0
        )

    def test_a_page_that_prints_long_is_planned_again_with_less_room(self) -> None:
        heights: list[float] = []

        def build(page: Page) -> tuple[ResumePlan, bytes]:
            heights.append(page.height)
            return self._plan(), _pages(1 if page.height < PAGE.height - 20 else 2)

        fitted = fit_to_page(build)
        assert fitted.fits and fitted.rounds == 2
        assert heights[1] < heights[0]

    def test_a_page_that_never_fits_says_so(self) -> None:
        fitted = fit_to_page(lambda page: (self._plan(), _pages(2)), max_rounds=3)
        assert (fitted.fits, fitted.rounds) == (False, 3)

    def test_a_one_page_print_has_no_overflow(self) -> None:
        assert overflow(_pages(1)) == 0.0
        assert overflow(_pages(2)) > 0.0


@pytest.mark.chromium
def test_an_estimate_that_overshoots_is_caught_by_the_print(
    snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
) -> None:
    """A budget that believes the page holds more than it does: the print runs to two pages,
    and the loop plans again until it prints on one."""
    import asyncio

    from aria_resume.render.html import render_html
    from aria_resume.render.pdf import ChromiumPrinter

    candidate = _named(snapshot)
    long = " ".join(
        ["Shipped eight full-stack applications with Node.js and PostgreSQL for regional grocers"] * 4
    )
    # Sixteen long facts, eight under each role, and a template that wants eight bullets on each.
    extra = tuple(
        candidate.facts[2 if index % 2 else 0].model_copy(
            update={"fact_id": uuid.UUID(int=900 + index), "text": f"{long} ({index})"}
        )
        for index in range(16)
    )
    candidate = candidate.model_copy(update={"facts": (*candidate.facts, *extra)})
    template = load_template(RoleType.BACKEND).model_copy(
        update={"older_role_bullets": (8, 8), "recent_role_bullets": (8, 8)}
    )
    normalized, evidence = _inputs("larkspur_backend", candidate, taxonomy)

    def build(page: Page) -> tuple[ResumePlan, bytes]:
        result = plan(normalized, evidence, candidate, taxonomy, template=template, page=page)
        html = render_html(fallback_document(result, candidate))
        return result, asyncio.run(ChromiumPrinter().print_pdf(html))

    optimistic = Page(height=1400.0)
    first, first_pdf = build(optimistic)
    assert overflow(first_pdf) > 0, "the optimistic plan was meant to overflow"
    fitted = fit_to_page(build, page=optimistic)
    assert fitted.fits and fitted.rounds > 1
    assert overflow(fitted.pdf) == 0.0
    assert len(fitted.plan.dropped) > len(first.dropped)
