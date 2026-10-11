"""The upload pipeline, and the things people send it (SECURITY.md §7).

Most of these are attacks or mistakes rather than happy paths, because the pipeline's
job is to be the thing that stops them. Each test says which rule it is checking.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from aria_api.uploads.detect import DetectedType, UnsupportedFileType, detect_type
from aria_api.uploads.quarantine import QuarantineStore, UploadTooLarge
from aria_api.uploads.scanner import (
    ClamAvScanner,
    DevScanner,
    ScannerMisconfigured,
    ScanStatus,
    assert_scanner_is_allowed,
    build_scanner,
)
from aria_core.config import Settings

APP_DSN = "postgresql+asyncpg://aria_app:x@127.0.0.1:5433/aria"


def _settings(**overrides: object) -> Settings:
    return Settings(database_url=SecretStr(APP_DSN), **overrides)  # type: ignore[arg-type]


# ------------------------------------------------------------------ type detection


def _docx_bytes(*, content_type: str | None = None) -> bytes:
    """A minimal OOXML package, as Word would write one."""
    declared = content_type or "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            f'<?xml version="1.0"?><Types><Override PartName="/word/document.xml" '
            f'ContentType="{declared}"/></Types>',
        )
        archive.writestr("word/document.xml", "<document><body/></document>")
    return buffer.getvalue()


class TestTypeComesFromContent:
    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            (b"%PDF-1.7\n...", DetectedType.PDF),
            (b"\x89PNG\r\n\x1a\n\x00\x00", DetectedType.PNG),
            (b"\xff\xd8\xff\xe0JFIF", DetectedType.JPEG),
            (b"Dileep Kumar Salla\nSoftware Engineer\n", DetectedType.TXT),
        ],
    )
    def test_each_accepted_type_is_recognised(self, content: bytes, expected: DetectedType) -> None:
        assert detect_type(content) is expected

    def test_a_real_docx_is_recognised_by_its_manifest(self) -> None:
        # Recognised when allowed. Acceptance is a separate decision from detection,
        # which is what lets the refusal below say what the file actually is.
        assert detect_type(_docx_bytes(), allow_docx=True) is DetectedType.DOCX

    def test_an_extension_cannot_override_the_content(self) -> None:
        # The classic: resume.pdf that is really something else. The name never reaches
        # the detector at all, which is the point.
        assert detect_type(b"\x89PNG\r\n\x1a\n") is DetectedType.PNG

    def test_a_macro_enabled_document_is_refused(self) -> None:
        # .docm declares a different content type. SECURITY.md §7 promises macros are
        # never executed; not accepting the format is the simplest way to keep that.
        macro_enabled = _docx_bytes(content_type="application/vnd.ms-word.document.macroEnabled.main+xml")
        with pytest.raises(UnsupportedFileType, match="not a Word document"):
            detect_type(macro_enabled)

    def test_a_plain_zip_is_refused(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("notes.txt", "hello")
        with pytest.raises(UnsupportedFileType, match="ZIP archive but not a Word document"):
            detect_type(buffer.getvalue())

    def test_an_empty_file_is_refused(self) -> None:
        with pytest.raises(UnsupportedFileType, match="empty"):
            detect_type(b"")

    def test_a_binary_file_is_not_mistaken_for_text(self) -> None:
        with pytest.raises(UnsupportedFileType, match="does not match any accepted type"):
            detect_type(b"\x7fELF\x02\x01\x01\x00payload")

    def test_text_with_a_nul_byte_is_refused(self) -> None:
        with pytest.raises(UnsupportedFileType):
            detect_type(b"Resume\x00hidden")

    def test_text_with_terminal_escapes_is_refused(self) -> None:
        # A "plain text" resume that repaints the terminal of whoever cats it.
        with pytest.raises(UnsupportedFileType):
            detect_type(b"Resume\x1b[2J\x1b[31mgotcha")

    def test_the_refusal_says_what_is_accepted(self) -> None:
        with pytest.raises(UnsupportedFileType, match="PDF and plain text"):
            detect_type(b"\x00\x01\x02\x03")

    def test_a_truncated_docx_is_refused_rather_than_crashing(self) -> None:
        with pytest.raises(UnsupportedFileType):
            detect_type(b"PK\x03\x04truncated-right-here")


class TestDocxIsDeclinedByDefault:
    """PDF and TXT only, for now (DECISIONS.md).

    A .docx is a zip of XML — XXE, zip bombs, external relationships — and every tool
    that writes one also writes a PDF. The reader stays built and tested so this can be
    revisited at beta without rebuilding anything.
    """

    def test_a_word_document_is_declined(self) -> None:
        with pytest.raises(UnsupportedFileType, match=r"not accepting \.docx") as caught:
            detect_type(_docx_bytes())
        assert "Save As" in caught.value.advice

    def test_the_advice_explains_how_to_convert_and_why_it_is_safe(self) -> None:
        # One extra step for the candidate, and the PDF keeps the links clickable —
        # which the parser now extracts from annotations.
        with pytest.raises(UnsupportedFileType) as caught:
            detect_type(_docx_bytes())
        assert "PDF" in caught.value.advice
        assert "clickable" in caught.value.advice

    def test_the_refusal_says_what_the_file_actually_is(self) -> None:
        # Not "invalid file": the detector knows, so the message says.
        with pytest.raises(UnsupportedFileType) as caught:
            detect_type(_docx_bytes())
        assert "Word document" in caught.value.detail

    def test_turning_the_flag_on_accepts_it_again(self) -> None:
        # A configuration change, not a code change.
        assert detect_type(_docx_bytes(), allow_docx=True) is DetectedType.DOCX

    def test_the_setting_defaults_to_off(self) -> None:
        assert _settings().allow_docx_uploads is False

    def test_pdf_and_text_are_unaffected(self) -> None:
        assert detect_type(b"%PDF-1.7 resume") is DetectedType.PDF
        assert detect_type(b"Skills: React, FastAPI\n") is DetectedType.TXT

    def test_a_macro_enabled_document_is_still_refused_with_the_flag_on(self) -> None:
        # The flag is about .docx, not about macros: a .docm never becomes acceptable.
        macro_enabled = _docx_bytes(content_type="application/vnd.ms-word.document.macroEnabled.main+xml")
        with pytest.raises(UnsupportedFileType, match="not a Word document"):
            detect_type(macro_enabled, allow_docx=True)


# --------------------------------------------------------------- the quarantine store


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


class TestQuarantineStore:
    @pytest.fixture
    def store(self, tmp_path: Path) -> QuarantineStore:
        return QuarantineStore(tmp_path / "quarantine", max_bytes=1024)

    async def test_a_file_is_stored_under_a_generated_name(self, store: QuarantineStore) -> None:
        stored = await store.store(_chunks(b"%PDF-1.7 hello"))
        assert stored.name.endswith(".bin")
        assert stored.path.parent == store.root
        assert stored.content == b"%PDF-1.7 hello"
        assert stored.size_bytes == 14

    async def test_the_hash_is_of_what_was_written(self, store: QuarantineStore) -> None:
        import hashlib

        stored = await store.store(_chunks(b"abc", b"def"))
        assert stored.sha256 == hashlib.sha256(b"abcdef").hexdigest()

    async def test_two_uploads_never_collide(self, store: QuarantineStore) -> None:
        first = await store.store(_chunks(b"same bytes"))
        second = await store.store(_chunks(b"same bytes"))
        assert first.name != second.name
        assert first.sha256 == second.sha256

    async def test_the_limit_is_enforced_while_streaming(self, store: QuarantineStore) -> None:
        # Checking after the fact would mean the oversized file has already been read
        # into memory, which is the denial of service the limit exists to prevent.
        with pytest.raises(UploadTooLarge, match="larger than"):
            await store.store(_chunks(b"x" * 600, b"x" * 600))

    async def test_an_over_limit_upload_leaves_nothing_behind(self, store: QuarantineStore) -> None:
        with pytest.raises(UploadTooLarge):
            await store.store(_chunks(b"x" * 2048))
        assert list(store.root.iterdir()) == []

    async def test_a_failing_stream_leaves_nothing_behind(self, store: QuarantineStore) -> None:
        async def exploding() -> AsyncIterator[bytes]:
            yield b"partial"
            raise OSError("the client went away")

        with pytest.raises(OSError, match="client went away"):
            await store.store(exploding())
        assert list(store.root.iterdir()) == []

    @pytest.mark.parametrize(
        "name",
        [
            "../../../etc/passwd",
            "..\\..\\windows\\system32\\config",
            "/absolute/path",
            "C:\\Windows\\System32\\drivers\\etc\\hosts",
            "..",
            ".",
            "with\x00nul.bin",
            "sub/dir.bin",
        ],
    )
    async def test_a_hostile_name_cannot_escape_the_store(self, store: QuarantineStore, name: str) -> None:
        # These never come from us — the stored name is a UUID — but read() builds a
        # path from a string, so it is the one place worth being certain.
        with pytest.raises(ValueError, match="quarantine"):
            store.read(name)
        assert store.exists(name) is False

    async def test_reading_back_gives_the_same_bytes(self, store: QuarantineStore) -> None:
        stored = await store.store(_chunks(b"%PDF-1.7 content"))
        assert store.read(stored.name) == b"%PDF-1.7 content"

    async def test_deleting_removes_the_original(self, store: QuarantineStore) -> None:
        stored = await store.store(_chunks(b"%PDF-1.7"))
        store.delete(stored.name)
        assert store.exists(stored.name) is False
        # Idempotent: the pipeline may delete after a refusal and again on cleanup.
        store.delete(stored.name)

    async def test_the_store_is_created_on_first_use(self, tmp_path: Path) -> None:
        store = QuarantineStore(tmp_path / "not-yet" / "quarantine", max_bytes=1024)
        assert not store.root.exists()
        await store.store(_chunks(b"%PDF-1.7"))
        assert store.root.exists()


# -------------------------------------------------------------------- the scanner


class TestTheDevScannerTellsTheTruth:
    async def test_it_reports_not_scanned_never_clean(self) -> None:
        # A scanner that says "clean" without looking puts a reassuring word in the
        # audit trail that the next reader takes as a fact.
        result = await DevScanner().scan(b"%PDF-1.7")
        assert result.status is ScanStatus.NOT_SCANNED
        assert result.is_clean is False
        assert "not scanned" in result.detail

    async def test_it_says_so_for_every_input(self) -> None:
        for content in (b"", b"%PDF-1.7", b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR"):
            assert (await DevScanner().scan(content)).status is ScanStatus.NOT_SCANNED

    def test_it_declares_that_it_does_not_scan(self) -> None:
        assert DevScanner().actually_scans is False

    def test_there_is_no_code_path_to_clean(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "aria_api" / "uploads" / "scanner.py").read_text(
            encoding="utf-8"
        )
        dev_section = source[source.index("class DevScanner") : source.index("class ClamAvScanner")]
        assert "ScanStatus.CLEAN" not in dev_section


class TestTheStartUpCheck:
    def test_the_dev_scanner_is_fine_in_dev(self) -> None:
        assert_scanner_is_allowed(DevScanner(), _settings(env="dev"))

    def test_the_dev_scanner_is_fine_in_ci(self) -> None:
        assert_scanner_is_allowed(DevScanner(), _settings(env="ci"))

    @pytest.mark.parametrize("env", ["staging", "prod"])
    def test_the_dev_scanner_is_refused_outside_dev(self, env: str) -> None:
        # The failure this prevents: a deployment that forgot to configure clamd,
        # accepting real files and recording them as unscanned — true, and unnoticed.
        with pytest.raises(ScannerMisconfigured, match="must be scanned"):
            assert_scanner_is_allowed(
                DevScanner(),
                _settings(
                    env=env,
                    llm_gateway_service_token=SecretStr("x"),
                    api_service_token=SecretStr("x"),
                    dev_kms_key_path=Path("/keys/managed.key"),
                    apply_task_signing_kid="prod-1",
                ),
            )

    def test_the_message_says_how_to_fix_it(self) -> None:
        with pytest.raises(ScannerMisconfigured, match="ARIA_CLAMAV_HOST"):
            assert_scanner_is_allowed(
                DevScanner(),
                _settings(
                    env="prod",
                    llm_gateway_service_token=SecretStr("x"),
                    api_service_token=SecretStr("x"),
                    dev_kms_key_path=Path("/keys/managed.key"),
                    apply_task_signing_kid="prod-1",
                ),
            )

    def test_a_real_scanner_passes_anywhere(self) -> None:
        assert_scanner_is_allowed(
            ClamAvScanner("127.0.0.1", 3310),
            _settings(
                env="prod",
                llm_gateway_service_token=SecretStr("x"),
                api_service_token=SecretStr("x"),
                dev_kms_key_path=Path("/keys/managed.key"),
                apply_task_signing_kid="prod-1",
            ),
        )


class TestScannerSelection:
    def test_no_host_means_the_dev_scanner(self) -> None:
        assert isinstance(build_scanner(_settings()), DevScanner)

    def test_a_host_means_clamav(self) -> None:
        scanner = build_scanner(_settings(clamav_host="127.0.0.1"))
        assert isinstance(scanner, ClamAvScanner)
        assert scanner.actually_scans is True

    async def test_an_unreachable_clamd_is_an_error_not_a_pass(self) -> None:
        # A scanner that cannot be reached must never look like a clean result.
        # Port 1 is reserved and nothing listens there.
        result = await ClamAvScanner("127.0.0.1", 1, timeout_seconds=2.0).scan(b"%PDF-1.7")
        assert result.status is ScanStatus.ERROR
        assert result.is_clean is False


class TestWhatMayBeParsed:
    @pytest.mark.parametrize(
        ("status", "allowed"),
        [
            (ScanStatus.CLEAN, True),
            (ScanStatus.NOT_SCANNED, True),
            (ScanStatus.INFECTED, False),
            (ScanStatus.ERROR, False),
        ],
    )
    def test_only_clean_or_unscanned_files_reach_the_parser(self, status: ScanStatus, allowed: bool) -> None:
        # NOT_SCANNED is allowed through so the feature can be built at all, which is
        # exactly why the start-up check keeps that combination out of production.
        assert status.may_be_parsed is allowed
