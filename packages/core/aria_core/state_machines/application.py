"""The application lifecycle (PRODUCT_SPEC §3.7).

This module holds the states. The legal transitions and the engine that validates
and audits them live in :mod:`aria_core.state_machines.engine`.

The spec writes the blocked and outcome states as one state with a reason
(``BLOCKED{captcha, login, closed, policy}``). They are separate states here,
because the reason decides what may happen next: a CAPTCHA block resumes once the
user solves it, while a closed posting is the end of the line. Putting that in the
transition table rather than in ``if`` statements keeps the rules in one readable
place.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ApplicationState"]


class ApplicationState(StrEnum):
    """Where one application is in its life.

    Stored as text with a CHECK constraint rather than a Postgres enum type, so
    adding a state is an ordinary migration instead of a type rewrite.
    """

    # ---- the main flow
    DISCOVERED = "discovered"
    SCREENING = "screening"
    ASK_USER = "ask_user"
    """Screening needs a policy decision from the user (an "ask me" posting type)."""
    TAILORING = "tailoring"
    VALIDATING = "validating"
    REPAIRING = "repairing"
    """A validator failed; the composer is rewriting the specific finding."""
    READY = "ready"
    AWAITING_APPROVAL = "awaiting_approval"
    """Approve mode, or a dream company, which is always Approve."""
    DISPATCHED_TO_RUNNER = "dispatched_to_runner"
    APPLYING = "applying"
    VERIFYING = "verifying"
    """Submitted according to the Runner; looking for proof before we believe it."""
    SUBMITTED = "submitted"
    """Only reachable with evidence and a receipt (PRODUCT_SPEC §1.5)."""
    FOLLOW_UP = "follow_up"

    # ---- outcomes
    OUTCOME_REJECTED = "outcome_rejected"
    OUTCOME_ASSESSMENT = "outcome_assessment"
    OUTCOME_INTERVIEW = "outcome_interview"
    OUTCOME_OFFER = "outcome_offer"
    OUTCOME_GHOSTED = "outcome_ghosted"

    # ---- exits
    SKIPPED = "skipped"
    BLOCKED_CAPTCHA = "blocked_captcha"
    BLOCKED_LOGIN = "blocked_login"
    BLOCKED_CLOSED = "blocked_closed"
    """The posting closed. Queued applications for it are cancelled."""
    BLOCKED_POLICY = "blocked_policy"
    FAILED = "failed"
    WAITING_FOR_USER = "waiting_for_user"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """A state nothing leaves. Checked by the engine, not by callers."""
        return self in _TERMINAL

    @property
    def is_outcome(self) -> bool:
        return self.name.startswith("OUTCOME_")

    @property
    def needs_user(self) -> bool:
        """States where the application waits for a human and never guesses."""
        return self in {
            ApplicationState.ASK_USER,
            ApplicationState.AWAITING_APPROVAL,
            ApplicationState.WAITING_FOR_USER,
            ApplicationState.BLOCKED_CAPTCHA,
            ApplicationState.BLOCKED_LOGIN,
        }


_TERMINAL: frozenset[ApplicationState] = frozenset(
    {
        ApplicationState.SKIPPED,
        ApplicationState.BLOCKED_CLOSED,
        ApplicationState.BLOCKED_POLICY,
        ApplicationState.FAILED,
        ApplicationState.CANCELLED,
        ApplicationState.OUTCOME_REJECTED,
        ApplicationState.OUTCOME_OFFER,
        ApplicationState.OUTCOME_GHOSTED,
    }
)
