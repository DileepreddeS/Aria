"""Signing and verifying an ApplyTask (SECURITY.md §8).

A task is permission to act on someone's behalf on a real job site, so the Runner
must be able to tell one the API issued from one anybody else produced. Ed25519:
small keys, small signatures, no parameter choices to get wrong, no padding modes.

Three details that matter more than the algorithm:

**The signature covers the exact bytes that travel.** The payload is signed and
transmitted as the same base64url blob, and verification checks the signature
against those bytes before parsing them. There is no canonicalisation step, and
therefore no gap between "what was signed" and "what was parsed" — the usual place
signature schemes go wrong.

**The key is named.** Every signature carries a ``kid``, so keys can be rotated
without a flag day: the Runner holds several public keys and uses the one named.
An unknown ``kid`` is a refusal, never a fallback to "try them all".

**Validity is checked, not assumed.** Expiry and not-before are verified against a
clock the caller can supply, and a task's lifetime is capped at construction
(:class:`aria_core.schemas.apply_task.ApplyTask`). Expiry alone does not stop a
task being used twice inside its window, which is what :mod:`.replay` is for.
"""

from __future__ import annotations

import base64
import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from aria_core.schemas.apply_task import ApplyTask
from aria_core.schemas.base import utc_now

__all__ = [
    "ALGORITHM",
    "SignedApplyTask",
    "TaskExpired",
    "TaskNotYetValid",
    "TaskVerificationError",
    "UnknownKeyId",
    "generate_signing_keypair",
    "load_private_key",
    "load_public_keys",
    "sign_task",
    "verify_task",
]

ALGORITHM: Final = "Ed25519"
#: Small tolerance for clock skew between the API and the user's device. Kept tight:
#: the task's whole lifetime is minutes.
CLOCK_SKEW: Final = dt.timedelta(seconds=30)


class TaskVerificationError(Exception):
    """A task was not accepted. Subclasses say why, for the audit record."""


class UnknownKeyId(TaskVerificationError):
    """The signature names a key this verifier does not hold."""


class TaskExpired(TaskVerificationError):
    pass


class TaskNotYetValid(TaskVerificationError):
    pass


class BadSignature(TaskVerificationError):
    pass


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


@dataclass(frozen=True, slots=True)
class SignedApplyTask:
    """A task as it travels: the signed bytes, the signature, and the key's name."""

    payload: str
    """base64url of the task's JSON. The exact bytes that were signed."""
    signature: str
    kid: str
    alg: str = ALGORITHM

    def to_dict(self) -> dict[str, str]:
        return {"payload": self.payload, "signature": self.signature, "kid": self.kid, "alg": self.alg}

    @classmethod
    def from_dict(cls, data: Mapping[str, str]) -> SignedApplyTask:
        return cls(
            payload=data["payload"],
            signature=data["signature"],
            kid=data["kid"],
            alg=data.get("alg", ALGORITHM),
        )


def sign_task(task: ApplyTask, private_key: Ed25519PrivateKey, *, kid: str) -> SignedApplyTask:
    """Serialise and sign one task."""
    payload = task.model_dump_json().encode("utf-8")
    return SignedApplyTask(
        payload=_b64encode(payload),
        signature=_b64encode(private_key.sign(payload)),
        kid=kid,
    )


def verify_task(
    signed: SignedApplyTask,
    public_keys: Mapping[str, Ed25519PublicKey],
    *,
    now: dt.datetime | None = None,
) -> ApplyTask:
    """Verify a signed task and return it, or raise.

    Order matters: the signature is checked before the payload is parsed, so no
    attacker-controlled bytes reach the parser on an unsigned task, and the
    timestamps that are checked afterwards are ones the API really issued.
    """
    if signed.alg != ALGORITHM:
        raise BadSignature(f"unsupported signature algorithm {signed.alg!r}")

    public_key = public_keys.get(signed.kid)
    if public_key is None:
        raise UnknownKeyId(f"no public key named {signed.kid!r}; keys are never guessed")

    try:
        payload = _b64decode(signed.payload)
        public_key.verify(_b64decode(signed.signature), payload)
    except (InvalidSignature, ValueError, TypeError) as error:
        raise BadSignature("the task's signature does not match its payload") from error

    task = ApplyTask.model_validate_json(payload)

    moment = now or utc_now()
    if moment + CLOCK_SKEW < task.issued_at:
        raise TaskNotYetValid(f"the task is issued at {task.issued_at.isoformat()}, which is in the future")
    if moment - CLOCK_SKEW >= task.expires_at:
        raise TaskExpired(f"the task expired at {task.expires_at.isoformat()}")

    return task


# --------------------------------------------------------------------------- keys


def generate_signing_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


def private_key_pem(private_key: Ed25519PrivateKey) -> bytes:
    """Unencrypted PKCS#8. The file's location and permissions protect it.

    A passphrase would have to be stored next to it to start a service unattended,
    which moves the problem rather than solving it. In production the key lives in a
    secret manager or a KMS and is never written to disk at all.
    """
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key_pem(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def load_private_key(pem: bytes) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"expected an Ed25519 private key, got {type(key).__name__}")
    return key


def load_public_keys(pems: Mapping[str, bytes]) -> dict[str, Ed25519PublicKey]:
    """kid → public key. The Runner holds this and nothing else."""
    keys: dict[str, Ed25519PublicKey] = {}
    for kid, pem in pems.items():
        key = serialization.load_pem_public_key(pem)
        if not isinstance(key, Ed25519PublicKey):
            raise TypeError(f"key {kid!r} is a {type(key).__name__}, not Ed25519")
        keys[kid] = key
    return keys
