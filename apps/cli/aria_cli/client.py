"""The HTTP client the CLI is made of.

Deliberately thin. Every method is one request, and none of them contains a decision
the API does not already make — the CLI must not be a second implementation of the
approval rules, because then "approve" would mean two different things depending on
how you asked.

An error from the API comes back as a message, not a traceback. The API's refusals are
written for a person to read ("I need a start and an end, like '2023-06 to 2023-08'"),
and a stack trace over the top of one throws away the only useful part.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx

from aria_core.api_contract import API_TOKEN_HEADER

__all__ = ["ApiError", "AriaClient"]


class ApiError(RuntimeError):
    """The API refused, and said why."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class AriaClient:
    """A session against a running API."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 120.0) -> None:
        # Parsing somebody's PDF in a subprocess is the slow part, so the default
        # timeout is generous. It is still a timeout: a hung request should fail.
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={API_TOKEN_HEADER: token},
            timeout=timeout,
        )

    def __enter__(self) -> AriaClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---------------------------------------------------------------- plumbing
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.ConnectError as error:
            raise ApiError(
                0,
                f"could not reach the API at {self._client.base_url}. Is it running? (./tasks.ps1 api)",
            ) from error
        except httpx.TimeoutException as error:
            raise ApiError(0, "the API did not respond in time") from error

        if response.is_success:
            return response.json() if response.content else {}

        detail = f"HTTP {response.status_code}"
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "detail" in body:
            found = body["detail"]
            # FastAPI's validation errors arrive as a list of per-field problems.
            detail = (
                "; ".join(
                    f"{'.'.join(str(part) for part in item.get('loc', []))}: {item.get('msg', '')}"
                    for item in found
                )
                if isinstance(found, list)
                else str(found)
            )
        raise ApiError(response.status_code, detail)

    # ------------------------------------------------------------------ health
    def health(self) -> dict[str, Any]:
        return dict(self._request("GET", "/health"))

    # ----------------------------------------------------------------- uploads
    def upload(self, path: Path, *, keep_original: bool = False) -> dict[str, Any]:
        with path.open("rb") as handle:
            return dict(
                self._request(
                    "POST",
                    "/uploads",
                    files={"file": (path.name, handle, "application/octet-stream")},
                    params={"keep_original": keep_original},
                )
            )

    def import_upload(self, upload_id: uuid.UUID | str, *, as_notes: bool = False) -> dict[str, Any]:
        return dict(self._request("POST", f"/uploads/{upload_id}/import", params={"as_notes": as_notes}))

    def lines(self, upload_id: uuid.UUID | str) -> dict[str, Any]:
        return dict(self._request("GET", f"/uploads/{upload_id}/lines"))

    # ------------------------------------------------------------------- facts
    def facts(self, status: str = "draft") -> dict[str, Any]:
        return dict(self._request("GET", "/facts", params={"status": status}))

    def approve(self, fact_id: uuid.UUID | str) -> dict[str, Any]:
        return dict(self._request("POST", f"/facts/{fact_id}/approve", json={}))

    def reject(self, fact_id: uuid.UUID | str, reason: str = "") -> dict[str, Any]:
        return dict(self._request("POST", f"/facts/{fact_id}/reject", json={"reason": reason}))

    def edit(self, fact_id: uuid.UUID | str, text: str, *, approve: bool = True) -> dict[str, Any]:
        return dict(self._request("POST", f"/facts/{fact_id}/edit", json={"text": text, "approve": approve}))

    def reconsider(self, fact_id: uuid.UUID | str) -> dict[str, Any]:
        return dict(self._request("POST", f"/facts/{fact_id}/reconsider", json={}))

    def merge(self, fact_id: uuid.UUID | str, keep_fact_id: uuid.UUID | str) -> dict[str, Any]:
        return dict(self._request("POST", f"/facts/{fact_id}/merge-into/{keep_fact_id}", json={}))

    # -------------------------------------------------------- graph entities
    def entities(self, kind: str, status: str = "draft") -> dict[str, Any]:
        return dict(self._request("GET", f"/entities/{kind}", params={"status": status}))

    def approve_entity(self, kind: str, entity_id: uuid.UUID | str) -> dict[str, Any]:
        return dict(self._request("POST", f"/entities/{kind}/{entity_id}/approve", json={}))

    def reject_entity(self, kind: str, entity_id: uuid.UUID | str, reason: str = "") -> dict[str, Any]:
        return dict(self._request("POST", f"/entities/{kind}/{entity_id}/reject", json={"reason": reason}))

    def reconsider_entity(self, kind: str, entity_id: uuid.UUID | str) -> dict[str, Any]:
        return dict(self._request("POST", f"/entities/{kind}/{entity_id}/reconsider", json={}))

    # --------------------------------------------------------------- questions
    def questions(self, status: str = "open") -> dict[str, Any]:
        return dict(self._request("GET", "/questions", params={"status": status}))

    def answer(self, question_id: uuid.UUID | str, answer: str) -> dict[str, Any]:
        return dict(self._request("POST", f"/questions/{question_id}/answer", json={"answer": answer}))

    def dismiss(self, question_id: uuid.UUID | str, reason: str = "") -> dict[str, Any]:
        return dict(self._request("POST", f"/questions/{question_id}/dismiss", json={"reason": reason}))

    # -------------------------------------------------------------- interviews
    def gaps(self) -> dict[str, Any]:
        return dict(self._request("GET", "/interview/gaps"))

    def enrichment(self, limit: int | None = None) -> dict[str, Any]:
        params = {"limit": limit} if limit is not None else None
        return dict(self._request("POST", "/interview/enrichment", params=params))

    def motivations(self) -> dict[str, Any]:
        return dict(self._request("POST", "/interview/motivations"))

    # ------------------------------------------------------------------ report
    def completeness(self) -> dict[str, Any]:
        return dict(self._request("GET", "/report/completeness"))
