"""The questions ARIA asks, written out in full.

Templates, not generated text. The decision is recorded in ``docs/DECISIONS.md``:
onboarding questions come from a fixed set rather than from a model. Three reasons,
and the third is the one that matters:

* the same graph always produces the same questions, so a gap that was asked about
  once is not asked about differently next time;
* no untrusted content reaches an instruction, because nothing is being generated;
* **a question is a prompt for a claim.** "Do you know how many teams used them?"
  invites a number that ends up on a resume, and a model inventing that question
  could just as easily invite a number the candidate never had. The wording of every
  question here was chosen by a person and can be argued with in review.

Each template carries a ``why`` as well as a prompt. A question whose purpose is
visible gets better answers, and "why are you asking me this?" is a fair question to
have an answer to.
"""

from __future__ import annotations

from dataclasses import dataclass

from aria_core.graph.status import MotivationKind, ProfileFieldKind

__all__ = [
    "ENRICHMENT_PROMPTS",
    "MOTIVATION_PROMPTS",
    "PROFILE_FIELD_PROMPTS",
    "Prompt",
    "QuestionTemplate",
]


@dataclass(frozen=True, slots=True)
class Prompt:
    """One question: what is asked, why, and what a usable answer looks like."""

    #: The question itself. ``{...}`` placeholders are filled from the graph.
    text: str
    #: Shown with the question. What it is for, and what happens to the answer.
    why: str
    #: A worked example, because "be specific" is not instruction enough.
    example: str = ""

    def render(self, **values: object) -> str:
        return self.text.format(**values)

    def detail(self) -> str:
        return f"{self.why} For example: {self.example}" if self.example else self.why


class QuestionTemplate:
    """The named templates. Strings live here; the logic that picks them does not."""

    MISSING_METRIC = "missing_metric"
    MISSING_CONTEXT = "missing_context"
    UNEVIDENCED_SKILL = "unevidenced_skill"
    MISSING_PROFILE_FIELD = "missing_profile_field"
    MOTIVATION = "motivation"


#: The enrichment interview. Each one targets a specific, checkable gap in the graph
#: — not "tell me more about yourself", which produces prose nobody can verify.
ENRICHMENT_PROMPTS: dict[str, Prompt] = {
    QuestionTemplate.MISSING_METRIC: Prompt(
        text=(
            'You wrote "{text}". Do you know a number for it \u2014 '
            "how many, how much faster, how much saved?"
        ),
        why=(
            "A bullet with a number survives scrutiny in a way one without does not, and "
            "a number you supply is the only kind ARIA will ever print. Your answer "
            "becomes a new draft fact for you to approve; the original is left alone."
        ),
        example='"Four teams used them" or "it cut the build from 12 minutes to 90 seconds"',
    ),
    QuestionTemplate.MISSING_CONTEXT: Prompt(
        text="What did you actually build or change at {employer} ({title})?",
        why=(
            "This role is on your graph with no achievements under it, so nothing can be "
            "tailored from it. One or two sentences is enough; each becomes a draft fact."
        ),
        example='"I rewrote the claims export as a scheduled job in Python"',
    ),
    QuestionTemplate.UNEVIDENCED_SKILL: Prompt(
        text="You list {skill} as a skill, but none of your facts mention it. What did you build with it?",
        why=(
            "A listed skill with nothing behind it is a keyword. ARIA will not count it "
            "towards years of experience and will not put it on a resume as evidence of "
            "anything, because there is no fact to cite. Your answer becomes a draft fact."
        ),
        example='"I used Kafka for the event pipeline at Accenture"',
    ),
}

#: Contact details an application cannot be submitted without. ARIA asks rather than
#: guessing, and never fills one in from a pattern — a wrong phone number on an
#: application is a lost interview, and a plausible wrong one is worse than a blank.
PROFILE_FIELD_PROMPTS: dict[ProfileFieldKind, Prompt] = {
    ProfileFieldKind.FULL_NAME: Prompt(
        text="What name should applications use for you?",
        why="It goes at the top of every resume and in every form.",
        example='"Jordan A. Rivera"',
    ),
    ProfileFieldKind.EMAIL: Prompt(
        text="Which email address should employers reply to?",
        why="Every application asks for one, and ARIA will not invent it.",
        example='"jordan.rivera@example.com"',
    ),
    ProfileFieldKind.PHONE: Prompt(
        text="What phone number should applications use?",
        why=(
            "Most forms require one. ARIA never guesses a phone number: a plausible "
            "wrong one is worse than a blank, because nobody notices it is wrong."
        ),
        example='"+1 469 555 0134"',
    ),
    ProfileFieldKind.LOCATION_CITY: Prompt(
        text="Which city are you based in?",
        why=(
            "Used to match roles by location and to answer the forms that ask. City and "
            "region only \u2014 ARIA has no field for a street address and does not keep one."
        ),
        example='"Portland"',
    ),
    ProfileFieldKind.LOCATION_REGION: Prompt(
        text="Which state or region?",
        why="The other half of the location most forms ask for.",
        example='"TX"',
    ),
}

#: The motivations interview. Fixed, in this order, and the same for everybody: these
#: are the things the Answer Engine needs for "why this role?" and they are the things
#: it must never invent. A motivation ARIA made up is a lie in the most personal part
#: of an application (PRODUCT_SPEC §3.4).
MOTIVATION_PROMPTS: dict[MotivationKind, Prompt] = {
    MotivationKind.PROBLEM_TYPE: Prompt(
        text="What kinds of problems do you actually enjoy working on?",
        why=(
            "The Answer Engine quotes this when a form asks why you want the role. It is "
            "never inferred from your resume: what you have done and what you like doing "
            "are different things."
        ),
        example='"Making slow things fast, and untangling systems nobody wants to touch"',
    ),
    MotivationKind.WANTS_NEXT: Prompt(
        text="What do you want from your next role that you do not have now?",
        why="Answers the most common open question on an application honestly.",
        example='"Owning a service end to end rather than one piece of one"',
    ),
    MotivationKind.TEAM_PREFERENCE: Prompt(
        text="What kind of team do you do your best work in?",
        why="Used for culture questions, and to rank postings you will actually like.",
        example='"Small, with code review that is actually read"',
    ),
    MotivationKind.COMPANY_PREFERENCE: Prompt(
        text="What kind of company are you looking for \u2014 size, stage, domain?",
        why="Filters the job search, and answers 'why us?' without flattery.",
        example='"Series B to D, anything but adtech"',
    ),
    MotivationKind.WORKING_STYLE: Prompt(
        text="How do you like to work day to day?",
        why="For the questions about process, pace and remote work.",
        example='"Long uninterrupted blocks; written decisions over meetings"',
    ),
    MotivationKind.AVOID: Prompt(
        text="What would make you turn a role down?",
        why=(
            "Just as useful as what you want, and never guessed. ARIA uses it to drop "
            "postings before you see them rather than after you have applied."
        ),
        example='"On-call without compensation, or a 100% legacy maintenance role"',
    ),
}
