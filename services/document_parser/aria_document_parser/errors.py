"""What can go wrong, and the difference between the two kinds.

:class:`ParseRefused` is a decision: the file is not something ARIA can use, and the
candidate is told why in words they can act on ("this PDF has no text layer — it is a
scan; please upload the version you exported from Word"). The upload is recorded as
refused.

:class:`ParseFailed` is a surprise: a malformed file, a library raising something
unexpected, a limit hit in a way that is not the candidate's fault to explain. It is
recorded as failed and is worth looking at.

The distinction matters because the first is a message to a person and the second is a
message to us.
"""

from __future__ import annotations

__all__ = ["ParseFailed", "ParseRefused"]


class ParseRefused(Exception):
    """ARIA will not use this file, and the candidate is told why.

    ``advice`` is the sentence shown to them. It says what to do next, because "parse
    error" is not something a person can act on.
    """

    def __init__(self, reason: str, advice: str) -> None:
        super().__init__(f"{reason}: {advice}")
        self.reason = reason
        self.advice = advice


class ParseFailed(Exception):
    """The parse did not finish and the reason is ours to investigate."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
