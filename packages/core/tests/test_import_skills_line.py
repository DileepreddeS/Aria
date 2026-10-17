"""Splitting and resolving a skills line (aria_core.importer.skills).

The regression: "AWS (EC2, S3, Lambda)" was split at the commas inside its parentheses
and "GitHub Actions CI/CD" at its slash, which reported "AWS (EC2", "GitHub Actions CI"
and "CD" as skills the taxonomy lacked, and kept the candidate's skills question open
over terms nobody wrote.
"""

from __future__ import annotations

import pytest

from aria_core.importer.lines import SourceLine
from aria_core.importer.skills import SkillFindings, parse_skill_list_line, split_skill_terms
from aria_core.taxonomy.loader import load_default_taxonomy
from aria_core.taxonomy.model import SkillTaxonomy


@pytest.fixture(scope="module")
def taxonomy() -> SkillTaxonomy:
    return load_default_taxonomy()


def _read(text: str, taxonomy: SkillTaxonomy) -> SkillFindings:
    return parse_skill_list_line(SourceLine(number=1, text=text, raw=text, bullet=False), taxonomy)


def _found(text: str, taxonomy: SkillTaxonomy) -> list[tuple[str, str]]:
    return [(skill.canonical, skill.surface_form) for skill in _read(text, taxonomy).skills]


class TestTheLinesThatWentWrong:
    def test_the_cloud_line(self, taxonomy: SkillTaxonomy) -> None:
        findings = _read(
            "Cloud & DevOps: AWS (EC2, S3, Lambda), Docker, GitHub Actions CI/CD, SLURM", taxonomy
        )
        assert findings.unknown == ()
        assert [skill.canonical for skill in findings.skills] == [
            "aws",
            # EC2 and S3 are AWS services, each its own skill (taxonomy 2026.10.3).
            "aws_ec2",
            "aws_s3",
            "lambda_functions",
            "docker",
            "github_actions",
            "ci_cd",
            "slurm",
        ]

    def test_the_ai_line(self, taxonomy: SkillTaxonomy) -> None:
        findings = _read("AI: Java Spring Boot, LangChain, vLLM, OpenAI API", taxonomy)
        assert findings.unknown == ()
        assert [skill.canonical for skill in findings.skills] == [
            "java",
            "spring_boot",
            "langchain",
            "vllm",
            "openai_api",
        ]


class TestParentheses:
    def test_a_group_stays_inside_its_term(self) -> None:
        assert split_skill_terms("AWS (EC2, S3, Lambda), Docker") == ["AWS (EC2, S3, Lambda)", " Docker"]

    def test_the_head_and_each_thing_named_under_it_are_resolved(self, taxonomy: SkillTaxonomy) -> None:
        assert _found("AWS (EC2, S3, Lambda)", taxonomy) == [
            ("aws", "AWS"),
            ("aws_ec2", "EC2"),
            ("aws_s3", "S3"),
            ("lambda_functions", "Lambda"),
        ]

    def test_unknown_names_inside_a_group_are_reported_one_by_one(self, taxonomy: SkillTaxonomy) -> None:
        findings = _read("Tools: Quuxly (Frobnitz, Zorblax)", taxonomy)
        assert findings.unknown == ("Quuxly", "Frobnitz", "Zorblax")

    def test_a_group_cut_off_by_a_line_break_does_not_leave_a_fragment(self, taxonomy: SkillTaxonomy) -> None:
        findings = _read("AWS (EC2, S3", taxonomy)
        assert findings.unknown == ()
        # Not "AWS (EC2" read as one term, which spells the alias "aws ec2".
        assert [skill.canonical for skill in findings.skills] == ["aws", "aws_ec2", "aws_s3"]


class TestSlashes:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("CI/CD", [("ci_cd", "CI/CD")]), ("TCP/IP", [("tcp_ip", "TCP/IP")])],
    )
    def test_a_slash_inside_a_skill_name_is_kept(
        self, taxonomy: SkillTaxonomy, text: str, expected: list[tuple[str, str]]
    ) -> None:
        assert _found(text, taxonomy) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("HTML/CSS", ["html", "css"]),
            ("C/C++", ["c", "cpp"]),
            ("GitHub Actions CI/CD", ["github_actions", "ci_cd"]),
            ("Java Spring Boot", ["java", "spring_boot"]),
        ],
    )
    def test_a_term_is_taken_as_parts_when_known_skills_cover_every_word(
        self, taxonomy: SkillTaxonomy, text: str, expected: list[str]
    ) -> None:
        assert [canonical for canonical, _ in _found(text, taxonomy)] == expected

    def test_parts_keep_the_resume_s_spelling(self, taxonomy: SkillTaxonomy) -> None:
        assert _found("GitHub Actions CI/CD", taxonomy) == [
            ("github_actions", "GitHub Actions"),
            ("ci_cd", "CI/CD"),
        ]

    @pytest.mark.parametrize(
        "text", ["Quuxly/Frobnitz", "GitHub Actions Frobnitz", "Python Wrangling", "Docker Python Frobnitz"]
    )
    def test_a_term_not_wholly_covered_is_reported_whole_never_as_a_fragment(
        self, taxonomy: SkillTaxonomy, text: str
    ) -> None:
        findings = _read(text, taxonomy)
        assert findings.unknown == (text,)
        assert findings.skills == ()


class TestOrdinarySeparatorsStillSplit:
    def test_commas_semicolons_bars_and_words(self, taxonomy: SkillTaxonomy) -> None:
        assert [canonical for canonical, _ in _found("Python, Go and SQL; R | Bash", taxonomy)] == [
            "python",
            "golang",
            "sql",
            "r_lang",
            "bash",
        ]
