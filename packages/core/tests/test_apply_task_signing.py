"""An ApplyTask is signed, short-lived and single-use (SECURITY.md §8)."""

from __future__ import annotations

import base64
import datetime as dt
import json
import uuid
from pathlib import Path
from typing import NamedTuple

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from aria_core.apply_task.keys_init import write_keypair
from aria_core.apply_task.replay import (
    TaskReplayed,
    consume_task,
    prune_consumed_tasks,
)
from aria_core.apply_task.signing import (
    ALGORITHM,
    BadSignature,
    SignedApplyTask,
    TaskExpired,
    TaskNotYetValid,
    UnknownKeyId,
    generate_signing_keypair,
    load_private_key,
    load_public_keys,
    private_key_pem,
    public_key_pem,
    sign_task,
    verify_task,
)
from aria_core.audit.log import read_chain
from aria_core.db.models import ConsumedApplyTaskJti
from aria_core.db.session import tenant_transaction
from aria_core.schemas.apply_task import ApplyTask
from aria_core.schemas.audit import AuditAction, AuditOutcome
from aria_core.schemas.base import utc_now
from aria_core.schemas.identity import TenantId, new_id
from aria_core.schemas.policy import AutonomyLevel, Capability
from aria_core.sensitivity import Sensitivity

KID = "dev-1"


def _task(tenant_id: uuid.UUID | None = None, **overrides: object) -> ApplyTask:
    issued = utc_now()
    fields: dict[str, object] = {
        "jti": new_id(),
        "tenant_id": tenant_id or new_id(),
        "application_id": new_id(),
        "issued_at": issued,
        "expires_at": issued + dt.timedelta(minutes=5),
        "capabilities": frozenset({Capability.NAVIGATE, Capability.FILL_FIELD}),
        "allowed_hosts": ("boards.greenhouse.io",),
        "target_url": "https://boards.greenhouse.io/acme/jobs/1",
        "autonomy_level": AutonomyLevel.APPROVE,
        "answer_keys": ("candidate.full_name", "candidate.email"),
    }
    return ApplyTask(**(fields | overrides))  # type: ignore[arg-type]


class Keyring(NamedTuple):
    """One signing key and the public keyring a Runner would hold."""

    private: Ed25519PrivateKey
    public: dict[str, Ed25519PublicKey]


@pytest.fixture
def keys() -> Keyring:
    private_key, public_key = generate_signing_keypair()
    return Keyring(private_key, {KID: public_key})


class TestTheTaskCarriesReferencesNotValues:
    def test_it_holds_no_sensitive_data(self) -> None:
        # A captured task must not be a copy of the candidate's profile.
        assert ApplyTask.max_sensitivity() <= Sensitivity.S1

    def test_it_names_answer_keys_rather_than_answers(self) -> None:
        task = _task()
        assert task.answer_keys == ("candidate.full_name", "candidate.email")
        # No field could hold a name, an email or an address.
        assert not {"full_name", "email", "phone", "address"} & set(ApplyTask.model_fields)

    def test_a_long_lived_task_is_refused(self) -> None:
        issued = utc_now()
        with pytest.raises(ValidationError, match="valid for minutes"):
            _task(issued_at=issued, expires_at=issued + dt.timedelta(hours=8))

    def test_an_inverted_window_is_refused(self) -> None:
        issued = utc_now()
        with pytest.raises(ValidationError, match="after issued_at"):
            _task(issued_at=issued, expires_at=issued - dt.timedelta(minutes=1))

    def test_uploading_requires_naming_the_file_hash(self) -> None:
        # UPLOAD_RESUME is only ever the PDF built for this application
        # (SECURITY.md §4), which the policy engine checks by hash.
        with pytest.raises(ValidationError, match="naming the file's hash"):
            _task(capabilities=frozenset({Capability.UPLOAD_RESUME}))

    def test_capabilities_are_explicit(self) -> None:
        assert Capability.SUBMIT_APPLICATION not in _task().capabilities


class TestSigning:
    def test_a_signed_task_verifies(self, keys: Keyring) -> None:
        private_key, public_keys = keys
        task = _task()
        verified = verify_task(sign_task(task, private_key, kid=KID), public_keys)
        assert verified == task

    def test_the_signature_names_its_key(self, keys: Keyring) -> None:
        private_key, _ = keys
        signed = sign_task(_task(), private_key, kid=KID)
        assert signed.kid == KID
        assert signed.alg == ALGORITHM

    def test_an_altered_payload_is_refused(self, keys: Keyring) -> None:
        private_key, public_keys = keys
        signed = sign_task(_task(), private_key, kid=KID)

        # Rewrite the target so the Runner would go somewhere else, keeping the
        # original signature.
        padding = "=" * (-len(signed.payload) % 4)
        payload = json.loads(base64.urlsafe_b64decode(signed.payload + padding))
        payload["target_url"] = "https://evil.test/collect"
        forged = SignedApplyTask(
            payload=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("="),
            signature=signed.signature,
            kid=signed.kid,
        )

        with pytest.raises(BadSignature):
            verify_task(forged, public_keys)

    def test_a_signature_from_another_task_is_refused(self, keys: Keyring) -> None:
        private_key, public_keys = keys
        first = sign_task(_task(), private_key, kid=KID)
        second = sign_task(_task(), private_key, kid=KID)

        with pytest.raises(BadSignature):
            verify_task(SignedApplyTask(first.payload, second.signature, KID), public_keys)

    def test_a_signature_from_another_key_is_refused(self, keys: Keyring) -> None:
        _, public_keys = keys
        other_private, _ = generate_signing_keypair()
        with pytest.raises(BadSignature):
            verify_task(sign_task(_task(), other_private, kid=KID), public_keys)

    def test_an_unknown_key_id_is_refused_rather_than_guessed(self, keys: Keyring) -> None:
        private_key, public_keys = keys
        signed = sign_task(_task(), private_key, kid="rotated-away")
        with pytest.raises(UnknownKeyId, match="never guessed"):
            verify_task(signed, public_keys)

    def test_an_unsupported_algorithm_is_refused(self, keys: Keyring) -> None:
        private_key, public_keys = keys
        signed = sign_task(_task(), private_key, kid=KID)
        with pytest.raises(BadSignature, match="unsupported"):
            verify_task(SignedApplyTask(signed.payload, signed.signature, KID, alg="none"), public_keys)

    def test_rotation_works_because_keys_are_named(self, keys: Keyring) -> None:
        old_private, public_keys = keys
        new_private, new_public = generate_signing_keypair()
        keyring = {**public_keys, "dev-2": new_public}

        # Both the old and the new key verify while both are published.
        verify_task(sign_task(_task(), old_private, kid=KID), keyring)
        verify_task(sign_task(_task(), new_private, kid="dev-2"), keyring)

    def test_garbage_is_refused_without_reaching_the_parser(self, keys: Keyring) -> None:
        _, public_keys = keys
        with pytest.raises(BadSignature):
            verify_task(SignedApplyTask("bm90IGpzb24", "bm90IGEgc2ln", KID), public_keys)


class TestValidityWindow:
    def test_an_expired_task_is_refused(self, keys: Keyring) -> None:
        private_key, public_keys = keys
        issued = utc_now() - dt.timedelta(minutes=10)
        signed = sign_task(
            _task(issued_at=issued, expires_at=issued + dt.timedelta(minutes=5)), private_key, kid=KID
        )
        with pytest.raises(TaskExpired):
            verify_task(signed, public_keys)

    def test_a_task_from_the_future_is_refused(self, keys: Keyring) -> None:
        private_key, public_keys = keys
        issued = utc_now() + dt.timedelta(minutes=10)
        signed = sign_task(
            _task(issued_at=issued, expires_at=issued + dt.timedelta(minutes=5)), private_key, kid=KID
        )
        with pytest.raises(TaskNotYetValid):
            verify_task(signed, public_keys)

    def test_a_small_clock_difference_is_tolerated(self, keys: Keyring) -> None:
        private_key, public_keys = keys
        task = _task()
        # The device's clock is 10 seconds behind the API's.
        verify_task(
            sign_task(task, private_key, kid=KID), public_keys, now=task.issued_at - dt.timedelta(seconds=10)
        )

    def test_the_caller_supplies_the_clock(self, keys: Keyring) -> None:
        private_key, public_keys = keys
        task = _task()
        with pytest.raises(TaskExpired):
            verify_task(
                sign_task(task, private_key, kid=KID),
                public_keys,
                now=task.expires_at + dt.timedelta(seconds=31),
            )


class TestKeyFiles:
    def test_a_keypair_round_trips_through_pem(self) -> None:
        private_key, public_key = generate_signing_keypair()
        reloaded_private = load_private_key(private_key_pem(private_key))
        reloaded_public = load_public_keys({KID: public_key_pem(public_key)})

        verify_task(sign_task(_task(), reloaded_private, kid=KID), reloaded_public)

    def test_keys_init_writes_a_private_key_and_a_public_keyring(self, tmp_path: Path) -> None:
        private_path, public_path = write_keypair(tmp_path / "signing.key", KID)

        assert b"PRIVATE KEY" in private_path.read_bytes()
        keyring = json.loads(public_path.read_text(encoding="utf-8"))
        assert set(keyring) == {KID}
        assert "PUBLIC KEY" in keyring[KID]
        # The private key is not in the file the Runner receives.
        assert "PRIVATE" not in public_path.read_text(encoding="utf-8")

    def test_an_existing_key_is_never_overwritten(self, tmp_path: Path) -> None:
        write_keypair(tmp_path / "signing.key", KID)
        with pytest.raises(FileExistsError, match="every task in flight"):
            write_keypair(tmp_path / "signing.key", KID)

    def test_a_key_may_not_be_written_inside_the_repository(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="inside the repository"):
            write_keypair(tmp_path / "keys" / "signing.key", KID, repo_root=tmp_path)

    def test_a_key_of_the_wrong_kind_is_refused(self) -> None:
        # A valid PEM is not enough: the algorithm has to be the one we verify with.
        rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with pytest.raises(TypeError, match="Ed25519"):
            load_private_key(rsa_pem)


@pytest.mark.db
class TestSingleUse:
    async def test_a_task_can_be_consumed_once(self, engine: AsyncEngine, tenant_a: TenantId) -> None:
        task = _task(tenant_a)
        async with tenant_transaction(engine, tenant_a) as session:
            await consume_task(session, task)

        async with tenant_transaction(engine, tenant_a) as session:
            spent = (await session.execute(select(ConsumedApplyTaskJti.jti))).scalars().all()
            events = await read_chain(session, tenant_a)

        assert list(spent) == [task.jti]
        assert events[-1].action == AuditAction.APPLY_TASK_CONSUMED.value
        assert events[-1].payload["application_id"] == str(task.application_id)

    async def test_a_second_use_is_refused(self, engine: AsyncEngine, tenant_a: TenantId) -> None:
        task = _task(tenant_a)
        async with tenant_transaction(engine, tenant_a) as session:
            await consume_task(session, task)

        with pytest.raises(TaskReplayed, match="already been consumed"):
            async with tenant_transaction(engine, tenant_a) as session:
                await consume_task(session, task)

    async def test_a_replay_is_audited_as_a_rejection(self, engine: AsyncEngine, tenant_a: TenantId) -> None:
        task = _task(tenant_a)
        async with tenant_transaction(engine, tenant_a) as session:
            await consume_task(session, task)

        # The rejection has to be recorded even though the call raises: a replayed
        # task is either a bug or an attempt, and both are worth noticing.
        async with tenant_transaction(engine, tenant_a) as session:
            with pytest.raises(TaskReplayed):
                await consume_task(session, task)

        async with tenant_transaction(engine, tenant_a) as session:
            events = await read_chain(session, tenant_a)

        rejections = [event for event in events if event.action == AuditAction.APPLY_TASK_REJECTED.value]
        assert len(rejections) == 1
        assert rejections[0].outcome == AuditOutcome.DENIED.value
        assert rejections[0].reason == "replayed"

    async def test_two_different_tasks_both_work(self, engine: AsyncEngine, tenant_a: TenantId) -> None:
        async with tenant_transaction(engine, tenant_a) as session:
            await consume_task(session, _task(tenant_a))
            await consume_task(session, _task(tenant_a))

        async with tenant_transaction(engine, tenant_a) as session:
            assert len((await session.execute(select(ConsumedApplyTaskJti))).scalars().all()) == 2

    async def test_a_task_cannot_be_consumed_under_another_tenant(
        self, engine: AsyncEngine, tenant_a: TenantId, tenant_b: TenantId
    ) -> None:
        task = _task(tenant_a)
        # Row-level security refuses the insert: the task's tenant is not the scope's.
        with pytest.raises(Exception, match=r"(?i)row-level security"):
            async with tenant_transaction(engine, tenant_b) as session:
                await consume_task(session, task)

    async def test_spent_ids_are_pruned_once_the_task_would_have_expired(
        self, engine: AsyncEngine, tenant_a: TenantId
    ) -> None:
        issued = utc_now() - dt.timedelta(minutes=20)
        old = _task(tenant_a, issued_at=issued, expires_at=issued + dt.timedelta(minutes=5))
        current = _task(tenant_a)

        async with tenant_transaction(engine, tenant_a) as session:
            await consume_task(session, old)
            await consume_task(session, current)

        async with tenant_transaction(engine, tenant_a) as session:
            removed = await prune_consumed_tasks(session)
            remaining = (await session.execute(select(ConsumedApplyTaskJti.jti))).scalars().all()

        assert removed == 1
        assert list(remaining) == [current.jti]
