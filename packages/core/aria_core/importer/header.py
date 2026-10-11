"""The resume header: name, contact details, links — and the address we throw away.

Every field here is proposed as a **draft** profile field. A resume header is the one
part of the document that is pure assertion about identity, and an importer that wrote
an email address straight into an approved field would have ARIA applying for jobs
under an address the candidate never confirmed. A stale phone number in a header is
common; a stale phone number on an application is a lost interview.

**Street addresses are discarded, deliberately and visibly.** ARIA has no
street-address field — there is no ``ProfileFieldKind`` for one, a test asserts that,
and SECURITY.md §3 classifies a home address as S2 data the product does not need. A
header line that carries one keeps its city and region and loses the rest, and the
import report says so, because "I dropped part of line 3" is something the candidate
should be told rather than left to discover.

Links come from two places and are treated the same way: URLs written in the text, and
URLs lifted out of the PDF's link annotations by the document parser (annotations are
where a real resume keeps them, which is why they are extracted at all). Neither is
ever fetched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from aria_core.graph.status import ProfileFieldKind
from aria_core.importer.lines import SourceLine

__all__ = ["DroppedDetail", "HeaderFindings", "ProposedProfileField", "parse_header"]

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

#: Deliberately loose on punctuation and strict on length: 7 to 15 digits, which is
#: the ITU range, so a date or an order number cannot pass as a phone number.
_PHONE = re.compile(r"(?<!\w)(\(?\+?\d[\d\s().\-]{5,18}\d)(?!\w)")

_URL = re.compile(
    r"\b(?:(?:https?://)|(?:www\.))[^\s,;<>\"')\]]+|\b[\w-]+\.(?:com|org|net|io|dev|ai|me|co|app|xyz|sh|page|github\.io)(?:/[^\s,;<>\"')\]]*)?",
    re.IGNORECASE,
)

#: "City, ST" or "City, Region". Two parts only — a third is usually a street.
_CITY_REGION = re.compile(
    r"(?P<city>[A-Z][A-Za-z.'\u2019-]+(?:\s+[A-Z][A-Za-z.'\u2019-]+){0,3})"
    r"\s*,\s*"
    r"(?P<region>[A-Z]{2}|[A-Z][A-Za-z.'\u2019-]+(?:\s+[A-Z][A-Za-z.'\u2019-]+){0,2})"
    r"(?!\w)"
)

#: A street address starts with a house number and names a thoroughfare. Matching the
#: suffix list rather than "any line with a number" keeps "Suite 200" out of a city.
_STREET = re.compile(
    r"\b\d+[A-Za-z]?\s+(?:[NSEW]\.?\s+)?[A-Za-z.'\u2019-]+(?:\s+[A-Za-z.'\u2019-]+)*?\s*"
    r"\b(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|boulevard|blvd|court|ct|circle|cir|"
    r"way|place|pl|terrace|ter|parkway|pkwy|highway|hwy|trail|trl|crossing|xing|loop|"
    r"apt|apartment|unit|suite|ste|#)\b\.?",
    re.IGNORECASE,
)

#: A unit written the other way round: "Apt 4B", "Suite 200", "#12". Part of the same
#: address, dropped for the same reason.
_UNIT = re.compile(
    r"\b(?:apt|apartment|unit|suite|ste|rm|room|fl|floor|#)\.?\s*#?\s*\w{1,6}\b",
    re.IGNORECASE,
)

#: How many lines after the top a name may still appear on. Beyond this it is not a
#: header any more, whatever it looks like.
_NAME_WINDOW = 6

#: Words that mean a line is a tagline rather than a name.
_NOT_A_NAME = re.compile(
    r"\b(?:engineer|developer|scientist|analyst|manager|intern|student|designer|"
    r"consultant|architect|resume|curriculum vitae|cv)\b",
    re.IGNORECASE,
)

_HOSTS: tuple[tuple[str, ProfileFieldKind], ...] = (
    ("linkedin.com", ProfileFieldKind.LINKEDIN_URL),
    ("github.com", ProfileFieldKind.GITHUB_URL),
    ("gitlab.com", ProfileFieldKind.GITHUB_URL),
    ("github.io", ProfileFieldKind.PORTFOLIO_URL),
)

#: Hosts that are somebody's profile elsewhere rather than the candidate's own site.
_OTHER_HOSTS = frozenset(
    {
        "twitter.com",
        "x.com",
        "medium.com",
        "dev.to",
        "stackoverflow.com",
        "kaggle.com",
        "huggingface.co",
        "scholar.google.com",
        "orcid.org",
        "leetcode.com",
    }
)


@dataclass(frozen=True, slots=True)
class ProposedProfileField:
    """One contact or identity field, to be created as a draft."""

    kind: ProfileFieldKind
    value: str
    source_line: int | None = None

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise ValueError("a profile field needs a value")


@dataclass(frozen=True, slots=True)
class DroppedDetail:
    """Something the importer removed on purpose, and why.

    Surfaced in the import report. An importer that quietly discards part of a line
    is indistinguishable from one with a bug.
    """

    source_line: int
    what: str
    reason: str


@dataclass(frozen=True, slots=True)
class HeaderFindings:
    fields: tuple[ProposedProfileField, ...] = ()
    dropped: tuple[DroppedDetail, ...] = ()


def _normalise_url(raw: str) -> str:
    """Give a bare URL a scheme so it can be parsed, and strip trailing punctuation."""
    cleaned = raw.rstrip(".,;:)]\u2019'\"")
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    return cleaned


def classify_url(raw: str) -> tuple[ProfileFieldKind, str] | None:
    """Which profile field a URL belongs in, with the URL normalised.

    ``None`` for a scheme ARIA will not store. The URL is classified by host and
    **never fetched** — it came out of an uploaded file, so it is untrusted input.
    """
    if raw.lower().startswith("mailto:"):
        # An email, not a link. The caller reads it as one; returning a portfolio URL
        # of "https://mailto:..." is the kind of nonsense that reaches a real form.
        return None

    url = _normalise_url(raw)
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None

    host = parts.netloc.lower().removeprefix("www.").split(":")[0]

    # A host needs a dot and a plausible top-level label. Without this check a bare
    # word becomes "https://word", which urlsplit is perfectly happy to parse — and
    # every word of a project description came back as the candidate's portfolio.
    labels = host.split(".")
    if len(labels) < 2 or not all(labels) or not (2 <= len(labels[-1]) <= 24):
        return None
    if not labels[-1].isalpha():
        return None

    for needle, kind in _HOSTS:
        if host == needle or host.endswith(f".{needle}") or needle in host:
            return kind, url
    if host in _OTHER_HOSTS:
        return ProfileFieldKind.OTHER_URL, url
    return ProfileFieldKind.PORTFOLIO_URL, url


def _looks_like_a_name(line: SourceLine) -> bool:
    text = line.text.strip()
    if not text or line.bullet or len(text.split()) > 5:
        return False
    if any(character.isdigit() for character in text):
        return False
    if _EMAIL.search(text) or _URL.search(text) or _NOT_A_NAME.search(text):
        return False
    if "," in text or "|" in text:
        return False
    return text[0].isupper()


def parse_header(lines: tuple[SourceLine, ...], links: tuple[str, ...] = ()) -> HeaderFindings:
    """Read the header block into proposed draft profile fields.

    ``links`` are the URLs the document parser lifted out of PDF annotations. They are
    attributed to no particular line, because an annotation belongs to a rectangle on
    a page rather than to a line of text.
    """
    fields: list[ProposedProfileField] = []
    dropped: list[DroppedDetail] = []
    kinds_seen: set[ProfileFieldKind] = set()

    def add(kind: ProfileFieldKind, value: str, source_line: int | None) -> None:
        # One of each: a header with two emails is a document the candidate should
        # look at, and picking one of them is exactly the kind of guess to avoid.
        if kind in kinds_seen or not value.strip():
            return
        kinds_seen.add(kind)
        fields.append(ProposedProfileField(kind=kind, value=value.strip()[:500], source_line=source_line))

    for line in lines[:_NAME_WINDOW]:
        if ProfileFieldKind.FULL_NAME not in kinds_seen and _looks_like_a_name(line):
            add(ProfileFieldKind.FULL_NAME, line.text, line.number)
            break

    for line in lines:
        if line.blank:
            continue
        text = line.text

        email = _EMAIL.search(text)
        if email:
            add(ProfileFieldKind.EMAIL, email.group(0), line.number)

        for match in _URL.finditer(text):
            classified = classify_url(match.group(0))
            if classified is not None:
                add(classified[0], classified[1], line.number)

        # The address comes out before the phone number, so a house number cannot be
        # read as one, and before the city so the street is not read as a city name.
        for pattern, what in ((_STREET, "street address"), (_UNIT, "apartment or unit")):
            address = pattern.search(text)
            if address is None:
                continue
            dropped.append(
                DroppedDetail(
                    source_line=line.number,
                    what=what,
                    reason=(
                        "ARIA has no field for a street address and does not keep one; "
                        "a form that asks for one comes to you"
                    ),
                )
            )
            text = text.replace(address.group(0), " ")

        phone = _PHONE.search(text)
        if phone and sum(character.isdigit() for character in phone.group(0)) >= 7:
            add(ProfileFieldKind.PHONE, " ".join(phone.group(0).split()), line.number)
            text = text.replace(phone.group(0), " ")

        location = _CITY_REGION.search(text)
        if location and ProfileFieldKind.LOCATION_CITY not in kinds_seen:
            add(ProfileFieldKind.LOCATION_CITY, location.group("city"), line.number)
            add(ProfileFieldKind.LOCATION_REGION, location.group("region"), line.number)

    for link in links:
        if link.lower().startswith("mailto:"):
            mailbox = link[len("mailto:") :].split("?")[0]
            if _EMAIL.fullmatch(mailbox):
                add(ProfileFieldKind.EMAIL, mailbox, None)

    for link in links:
        classified = classify_url(link)
        if classified is not None:
            add(classified[0], classified[1], None)

    return HeaderFindings(tuple(fields), tuple(dropped))
