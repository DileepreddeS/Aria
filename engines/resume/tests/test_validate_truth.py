"""6b Truth check, the deterministic part (RESUME_ENGINE §9).

The rule under test: a bullet may say what its cited facts say and nothing more. Each
test below adds one thing a writer might add (a number, a technology, an employer, a
scope, a month) and requires a finding; the facts' own words always pass, because the
repair loop's last resort is the fact text itself.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from aria_resume.document import (
    DocumentBullet,
    DocumentEntry,
    DocumentSection,
    ResumeDocument,
    SectionKind,
)
from aria_resume.schemas import Bullet
from aria_resume.validate import Finding
from aria_resume.validate.truth import check_bullet_text, check_truth, check_written

from aria_core.schemas.snapshot import CandidateSnapshot, SnapshotSkill
from aria_core.taxonomy.model import SkillTaxonomy

Judge = Callable[..., list[Finding]]


@pytest.fixture
def judge(snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy) -> Judge:
    def run(text: str, *fact_ids: uuid.UUID) -> list[Finding]:
        return check_bullet_text(text, fact_ids, snapshot, taxonomy)

    return run


def _checks(findings: list[Finding]) -> set[str]:
    return {finding.check for finding in findings}


class TestTheFactsOwnWordsPass:
    def test_every_fact_s_text_as_a_bullet(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        for fact in snapshot.facts:
            assert check_bullet_text(fact.text, (fact.fact_id,), snapshot, taxonomy) == [], fact.text

    def test_a_rewording_that_adds_nothing(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert judge("Cut p95 API latency 30% by adding Redis caching", ids.latency) == []

    def test_two_facts_together(self, judge: Judge, ids: SimpleNamespace) -> None:
        text = "Built 15+ React screens and cut p95 API latency by 30% with Redis caching"
        assert judge(text, ids.screens, ids.latency) == []

    def test_another_spelling_of_the_cited_skill(self, judge: Judge, ids: SimpleNamespace) -> None:
        # The fact says PostgreSQL; Postgres is the same skill (an alias), so the same claim.
        assert judge("Shipped 8 full-stack applications with Node.js and Postgres", ids.apps) == []


class TestCitations:
    def test_a_bullet_must_cite_a_fact(self, judge: Judge) -> None:
        assert _checks(judge("Built 15+ React screens")) == {"no_citation"}

    def test_a_fact_the_snapshot_does_not_hold(self, judge: Judge) -> None:
        assert "unknown_fact" in _checks(judge("Built 15+ React screens", uuid.UUID(int=999)))


class TestNumbers:
    def test_a_number_no_cited_fact_states(self, judge: Judge, ids: SimpleNamespace) -> None:
        findings = judge("Built 15+ React screens used by 40 teams", ids.screens)
        assert [finding.check for finding in findings] == ["unbacked_number"]
        assert "40" in findings[0].detail

    @pytest.mark.parametrize("changed", ["30 percent", "31%", "30.0%", "~30%", "over 30%"])
    def test_a_metric_reworded_rounded_or_converted(
        self, judge: Judge, ids: SimpleNamespace, changed: str
    ) -> None:
        assert "unbacked_number" in _checks(
            judge(f"Cut p95 API latency by {changed} with Redis caching", ids.latency)
        )

    def test_a_plus_dropped_or_added(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert "unbacked_number" in _checks(judge("Built 15 React screens", ids.screens))
        assert "unbacked_number" in _checks(judge("Shipped 8+ full-stack applications", ids.apps))

    def test_a_number_from_a_fact_the_bullet_does_not_cite(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert "unbacked_number" in _checks(
            judge("Built 15+ React screens, cutting latency 30%", ids.screens)
        )

    def test_a_team_size_in_words(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert "unbacked_number" in _checks(judge("Built 15+ React screens with five engineers", ids.screens))

    def test_a_number_word_the_fact_says_passes(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert judge("Led three engineers moving billing to Python 3.12", ids.billing) == []

    def test_a_multiplier_word(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert "unbacked_number" in _checks(
            judge("Doubled checkout speed with 15+ React screens", ids.screens)
        )

    def test_a_year(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert "unbacked_number" in _checks(judge("Built 15+ React screens in 2024", ids.screens))

    def test_a_name_with_a_digit_is_not_a_number(self, judge: Judge, ids: SimpleNamespace) -> None:
        # "p95" is a percentile's name, as "S3" or "EC2" would be: not a claim of quantity.
        assert judge("Cut p95 API latency by 30% with Redis caching", ids.latency) == []


class TestDates:
    def test_a_month_no_cited_fact_names(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert _checks(judge("Built 15+ React screens by June", ids.screens)) == {"unbacked_date"}

    def test_a_month_the_fact_names_passes(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert judge("Led three engineers moving billing to Python 3.12 in March 2023", ids.billing) == []

    def test_may_the_verb_is_not_a_month(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert "unbacked_date" not in _checks(
            judge("Built 15+ React screens any team may reuse", ids.screens)
        )


class TestTechnology:
    def test_a_technology_no_cited_fact_uses(self, judge: Judge, ids: SimpleNamespace) -> None:
        findings = judge("Cut p95 API latency by 30% with Redis caching on Kubernetes", ids.latency)
        assert [finding.check for finding in findings] == ["unbacked_technology"]
        assert "Kubernetes" in findings[0].detail

    def test_a_hyphenated_technology_no_cited_fact_uses(self, judge: Judge, ids: SimpleNamespace) -> None:
        # "React-based" claims React. The tagger once read it as one unknown word, so the
        # check passed a technology claim it never verified (DECISIONS 2026-10-18).
        findings = judge(
            "Cut p95 API latency by 30% with Redis caching behind a React-based dashboard", ids.latency
        )
        assert [finding.check for finding in findings] == ["unbacked_technology"]
        assert "'React'" in findings[0].detail

    def test_a_hyphenated_technology_the_cited_fact_uses(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert judge("Built 15+ React-based screens for the checkout platform", ids.screens) == []

    def test_a_technology_another_fact_uses(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert "unbacked_technology" in _checks(judge("Built 15+ React screens backed by Redis", ids.screens))

    def test_a_sibling_product_the_taxonomy_files_under_the_same_id(
        self, judge: Judge, ids: SimpleNamespace
    ) -> None:
        # The taxonomy lists Memcached as an alias of redis, for matching. A Redis fact is
        # not evidence of Memcached.
        findings = judge("Cut p95 API latency by 30% with Memcached caching", ids.latency)
        assert _checks(findings) == {"unbacked_technology"}
        assert "Memcached" in findings[0].detail

    def test_implication_runs_one_way(self, judge: Judge, ids: SimpleNamespace) -> None:
        # React implies JavaScript; JavaScript does not imply Next.js.
        assert "unbacked_technology" in _checks(judge("Built 15+ Next.js screens", ids.screens))

    @pytest.mark.parametrize(
        ("text", "fact"),
        [
            # PostgreSQL implies SQL, React implies JavaScript: true of the technology,
            # and allowed on the skills line, but in a bullet a claim about what was built.
            ("Shipped 8 full-stack applications with Node.js and SQL", "apps"),
            ("Built 15+ JavaScript screens for the checkout platform", "screens"),
        ],
    )
    def test_an_implied_skill_is_never_named_in_a_bullet(
        self, judge: Judge, ids: SimpleNamespace, text: str, fact: str
    ) -> None:
        assert _checks(judge(text, getattr(ids, fact))) == {"unbacked_technology"}

    def test_a_stored_tag_is_read_again_through_its_spelling(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, ids: SimpleNamespace
    ) -> None:
        # Tagged before taxonomy 2026.10.3, when Memcached was filed under redis: the
        # stored id says redis, the candidate's spelling says Memcached.
        old = snapshot.facts[0].model_copy(
            update={
                "text": "Cut p95 API latency by 30% with a cache",
                "skills": (SnapshotSkill(canonical="redis", surface_form="Memcached"),),
            }
        )
        retagged = snapshot.model_copy(update={"facts": (old, *snapshot.facts[1:])})
        assert (
            check_bullet_text("Cut p95 API latency by 30% with Memcached", (ids.latency,), retagged, taxonomy)
            == []
        )
        findings = check_bullet_text(
            "Cut p95 API latency by 30% with Redis", (ids.latency,), retagged, taxonomy
        )
        assert _checks(findings) == {"unbacked_technology"}

    def test_a_stored_tag_whose_spelling_names_nothing_backs_nothing(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, ids: SimpleNamespace
    ) -> None:
        # 2026.10.2 read "checkout" as payments; the word names no skill now.
        old = snapshot.facts[1].model_copy(
            update={"skills": (SnapshotSkill(canonical="payments", surface_form="checkout"),)}
        )
        retagged = snapshot.model_copy(update={"facts": (snapshot.facts[0], old, *snapshot.facts[2:])})
        findings = check_bullet_text("Built 15+ payments screens", (ids.screens,), retagged, taxonomy)
        assert "unbacked_technology" in _checks(findings)


class TestNamesAndScope:
    def test_another_employer(self, judge: Judge, ids: SimpleNamespace) -> None:
        findings = judge("Built 15+ React screens with Brightfold Systems", ids.screens)
        assert [finding.check for finding in findings] == ["unbacked_name"]

    def test_another_project(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert "unbacked_name" in _checks(judge("Built 15+ React screens for VERA-RAG", ids.screens))

    def test_another_role_s_title(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert "unbacked_name" in _checks(
            judge("Built 15+ React screens as Associate Software Engineer", ids.screens)
        )

    def test_the_cited_fact_s_own_employer_passes(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert judge("Built 15+ React screens at Nordvale Consulting", ids.screens) == []

    def test_the_cited_fact_s_own_title_is_not_read_as_the_shorter_one_inside_it(
        self, judge: Judge, ids: SimpleNamespace
    ) -> None:
        # "Associate Software Engineer" (Brightfold, cited) contains "Software Engineer"
        # (Nordvale, not cited); the longer, backed name covers it.
        assert judge("Shipped 8 full-stack applications as Associate Software Engineer", ids.apps) == []

    @pytest.mark.parametrize(
        "claim",
        [
            "Led a squad building 15+ React screens",
            "Mentored juniors on 15+ React screens",
            "Owned 15+ React screens",
        ],
    )
    def test_leadership_the_facts_do_not_state(self, judge: Judge, ids: SimpleNamespace, claim: str) -> None:
        assert "unbacked_scope" in _checks(judge(claim, ids.screens))

    def test_leadership_the_fact_states_passes(self, judge: Judge, ids: SimpleNamespace) -> None:
        assert judge("Led three engineers moving billing to Python 3.12", ids.billing) == []

    @pytest.mark.parametrize(
        "claim",
        [
            "Built 15+ company-wide React screens",
            "Single-handedly built 15+ React screens",
            "Built 15+ React screens used globally",
        ],
    )
    def test_a_scope_the_facts_do_not_state(self, judge: Judge, ids: SimpleNamespace, claim: str) -> None:
        assert "unbacked_scope" in _checks(judge(claim, ids.screens))


def _document(*sections: DocumentSection) -> ResumeDocument:
    return ResumeDocument(name="Jordan A. Rivera", sections=sections)


def _summary(*lines: str) -> DocumentSection:
    return DocumentSection(kind=SectionKind.SUMMARY, lines=lines)


class TestTheWholeDocument:
    def test_every_bullet_on_the_page_is_checked(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, ids: SimpleNamespace
    ) -> None:
        entry = DocumentEntry(
            title="Nordvale Consulting",
            bullets=(
                DocumentBullet(text="Built 15+ React screens", fact_ids=(ids.screens,)),
                DocumentBullet(text="Built 40 React screens", fact_ids=(ids.screens,)),
            ),
        )
        document = _document(DocumentSection(kind=SectionKind.EXPERIENCE, entries=(entry,)))
        findings = check_truth(document, snapshot, taxonomy)
        assert [finding.check for finding in findings] == ["unbacked_number"]
        assert "Built 40 React screens" in findings[0].detail

    @pytest.mark.parametrize(
        "line",
        [
            # The clause ends at the comma: three years of experience, then the skills.
            "Software engineer with 3 years of full-time experience, working in React and Redis.",
            "Software engineer with three years of experience, building with Python.",
            "Software engineer with 1+ year of React.",
        ],
    )
    def test_a_summary_the_graph_supports(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, line: str
    ) -> None:
        assert check_truth(_document(_summary(line)), snapshot, taxonomy) == []

    @pytest.mark.parametrize(
        ("line", "check"),
        [
            # 36 full-time months: three years, not four.
            ("Software engineer with 4 years of experience.", "summary_years"),
            ("Software engineer with 3.5 years of experience.", "summary_years"),
            ("Software engineer with five years of experience.", "summary_years"),
            # Python has 6 full-time months behind it, whatever the career total.
            ("Software engineer with 2 years of Python.", "summary_years"),
            ("Software engineer with 2+ years of Python experience.", "summary_years"),
            # Three years of experience *in React*: React has 18 months behind it.
            ("Software engineer with 3 years of experience in React.", "summary_years"),
            # Kubernetes is in no approved fact.
            ("Software engineer working in React and Kubernetes.", "unbacked_technology"),
            # TypeScript is declared but no fact uses it: a summary names evidenced skills.
            ("Software engineer working in TypeScript.", "unbacked_technology"),
            # JavaScript only by implication from React: the skills line may list it, the
            # summary may not name it.
            ("Software engineer working in JavaScript.", "unbacked_technology"),
            ("Software engineer who cut latency by 45%.", "unbacked_number"),
            ("Software engineer who led company-wide platform work.", "unbacked_scope"),
        ],
    )
    def test_a_summary_claim_the_graph_does_not_support(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, line: str, check: str
    ) -> None:
        assert check in _checks(check_truth(_document(_summary(line)), snapshot, taxonomy))

    def test_a_summary_may_cite_any_approved_metric(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        line = "Software engineer who cut p95 API latency by 30% with Redis."
        assert check_truth(_document(_summary(line)), snapshot, taxonomy) == []

    def test_the_skills_lines_hold_verified_skills_only(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        good = DocumentSection(
            kind=SectionKind.SKILLS, lines=("Languages: Python, TypeScript, SQL", "Data: PostgreSQL, Redis")
        )
        assert check_truth(_document(good), snapshot, taxonomy) == []
        bad = DocumentSection(kind=SectionKind.SKILLS, lines=("Languages: Python, Rust", "Tools: Kubernetes"))
        findings = check_truth(_document(bad), snapshot, taxonomy)
        assert [finding.check for finding in findings] == ["unverified_skill", "unverified_skill"]
        assert {"Rust", "Kubernetes"} == {finding.detail.split("'")[1] for finding in findings}

    def test_an_allowed_implication_may_be_listed(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        # React -> JavaScript and PostgreSQL -> SQL are on implied_skills.yaml: one step,
        # and true of the technology.
        line = DocumentSection(kind=SectionKind.SKILLS, lines=("Languages: Python, JavaScript, SQL",))
        assert check_truth(_document(line), snapshot, taxonomy) == []

    @pytest.mark.parametrize(
        "listed",
        [
            # RAG implies LLMs in the taxonomy, one step, but it is an inference about the
            # candidate rather than a fact about RAG: not on the list.
            "AI: LLMs",
            # RAG -> embeddings -> deep learning: two steps, never.
            "AI: deep learning",
        ],
    )
    def test_an_implication_off_the_list_is_not_listed(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, listed: str
    ) -> None:
        line = DocumentSection(kind=SectionKind.SKILLS, lines=(listed,))
        assert _checks(check_truth(_document(line), snapshot, taxonomy)) == {"unverified_skill"}

    def test_a_skills_term_the_taxonomy_does_not_know_is_refused(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy
    ) -> None:
        line = DocumentSection(kind=SectionKind.SKILLS, lines=("Languages: Python, Zorblang",))
        assert _checks(check_truth(_document(line), snapshot, taxonomy)) == {"unverified_skill"}


class TestTheWritersDeclarations:
    def _bullet(self, ids: SimpleNamespace, **changes: object) -> Bullet:
        values: dict[str, object] = {
            "text": "Cut p95 API latency by 30% with Redis caching",
            "fact_ids": (ids.latency,),
            "metrics_used": ("30%",),
            "tech_used": ("Redis",),
            "requirement_ids": ("r1",),
        }
        values.update(changes)
        return Bullet.model_validate(values)

    def test_an_honest_declaration_passes(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, ids: SimpleNamespace
    ) -> None:
        assert check_written(self._bullet(ids), snapshot, taxonomy, provided=(ids.latency,)) == []

    def test_citing_a_fact_the_slot_was_not_given(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, ids: SimpleNamespace
    ) -> None:
        findings = check_written(self._bullet(ids), snapshot, taxonomy, provided=(ids.screens,))
        assert "not_provided" in _checks(findings)

    def test_a_declared_metric_no_cited_fact_holds(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, ids: SimpleNamespace
    ) -> None:
        bullet = self._bullet(ids, metrics_used=("30%", "15+"))
        assert "metric_not_in_facts" in _checks(
            check_written(bullet, snapshot, taxonomy, provided=(ids.latency,))
        )

    def test_a_declared_metric_the_text_does_not_print(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, ids: SimpleNamespace
    ) -> None:
        bullet = self._bullet(ids, text="Cut p95 API latency with Redis caching")
        assert "metric_not_in_text" in _checks(
            check_written(bullet, snapshot, taxonomy, provided=(ids.latency,))
        )

    def test_a_metric_printed_but_not_declared(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, ids: SimpleNamespace
    ) -> None:
        bullet = self._bullet(ids, metrics_used=())
        assert "metric_not_declared" in _checks(
            check_written(bullet, snapshot, taxonomy, provided=(ids.latency,))
        )

    def test_a_declared_technology_outside_the_allowed_list(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, ids: SimpleNamespace
    ) -> None:
        # Memcached resolves to the same taxonomy id as Redis; it is still another product.
        bullet = self._bullet(ids, tech_used=("Redis", "Memcached"))
        assert "tech_not_allowed" in _checks(
            check_written(bullet, snapshot, taxonomy, provided=(ids.latency,))
        )
        bullet = self._bullet(ids, tech_used=("Redis", "Kubernetes"))
        assert "tech_not_allowed" in _checks(
            check_written(bullet, snapshot, taxonomy, provided=(ids.latency,))
        )

    def test_the_text_is_checked_as_well(
        self, snapshot: CandidateSnapshot, taxonomy: SkillTaxonomy, ids: SimpleNamespace
    ) -> None:
        bullet = self._bullet(ids, text="Cut p95 API latency by 30% with Redis caching for 12 teams")
        assert "unbacked_number" in _checks(
            check_written(bullet, snapshot, taxonomy, provided=(ids.latency,))
        )

    def test_a_bullet_must_cite_at_least_one_fact(self, ids: SimpleNamespace) -> None:
        with pytest.raises(ValueError, match="fact_ids"):
            self._bullet(ids, fact_ids=())
