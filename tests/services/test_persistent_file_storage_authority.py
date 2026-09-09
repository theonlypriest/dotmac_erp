"""Focused behavior proof for the persistent-file object-storage cutover."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.finance.automation.document_template import TemplateType
from app.models.finance.rpt.report_instance import ReportStatus
from app.services.automation.document_generator import (
    DocumentGeneratorService,
    DocumentStorageError,
)
from app.services.file_upload import FileStorageError, UploadResult
from app.services.finance.rpt.report_instance import ReportInstanceService
from app.services.people.hr.handbook_service import (
    HRDocumentService,
    HRDocumentStorageUnavailableError,
)


def _upload_result(key: str, payload: bytes) -> UploadResult:
    filename = key.rsplit("/", 1)[-1]
    return UploadResult(
        s3_key=key,
        relative_path=key.split("/", 1)[-1],
        filename=filename,
        file_size=len(payload),
        checksum="a" * 64,
    )


def test_handbook_upload_returns_only_an_opaque_object_reference() -> None:
    org_id = uuid4()
    payload = b"%PDF-1.7 handbook"
    upload = MagicMock()
    upload.save.return_value = _upload_result(
        f"hr_documents/{org_id}/object.pdf", payload
    )

    with patch(
        "app.services.people.hr.handbook_service.get_hr_handbook_upload",
        return_value=upload,
    ):
        reference, size, checksum = HRDocumentService(MagicMock()).save_document_file(
            org_id, "handbook.pdf", payload, content_type="application/pdf"
        )

    assert reference == f"hr_documents/{org_id}/object.pdf"
    assert size == len(payload)
    assert checksum == "a" * 64
    upload.save.assert_called_once_with(
        payload,
        content_type="application/pdf",
        subdirs=(str(org_id),),
        original_filename="handbook.pdf",
    )


def test_handbook_upload_does_not_create_metadata_when_storage_fails() -> None:
    upload = MagicMock()
    upload.save.side_effect = FileStorageError("Object storage upload failed")

    with (
        patch(
            "app.services.people.hr.handbook_service.get_hr_handbook_upload",
            return_value=upload,
        ),
        pytest.raises(
            HRDocumentStorageUnavailableError,
            match="temporarily unavailable",
        ),
    ):
        HRDocumentService(MagicMock()).save_document_file(
            uuid4(), "handbook.pdf", b"%PDF-1.7 handbook"
        )


def _report_generation_state() -> tuple[MagicMock, SimpleNamespace, SimpleNamespace]:
    org_id = uuid4()
    instance = SimpleNamespace(
        instance_id=uuid4(),
        organization_id=org_id,
        report_def_id=uuid4(),
        parameters_used={},
        fiscal_period_id=None,
        status=ReportStatus.QUEUED,
        started_at=None,
        generated_by_user_id=uuid4(),
    )
    definition = SimpleNamespace(
        report_def_id=instance.report_def_id,
        organization_id=org_id,
    )
    db = MagicMock()
    db.get.side_effect = [instance, definition]
    return db, instance, definition


def test_report_json_completes_only_with_an_s3_reference() -> None:
    db, instance, _definition = _report_generation_state()
    payload = {"rows": [{"amount": 10}]}
    encoded = json.dumps(payload, default=str, indent=2).encode("utf-8")
    upload = MagicMock()
    upload.save.return_value = _upload_result(
        f"generated_reports/{instance.organization_id}/report.json", encoded
    )

    with (
        patch.object(
            ReportInstanceService,
            "_commit_and_refresh",
            side_effect=lambda _db, value: value,
        ),
        patch.object(ReportInstanceService, "_generate_payload", return_value=payload),
        patch.object(
            ReportInstanceService,
            "complete_generation",
            return_value=instance,
        ) as complete,
        patch(
            "app.services.finance.rpt.report_instance.get_generated_report_upload",
            return_value=upload,
        ),
    ):
        result = ReportInstanceService.generate_report(
            db,
            instance.organization_id,
            instance.instance_id,
            instance.generated_by_user_id,
        )

    assert result is instance
    complete.assert_called_once_with(
        db=db,
        instance_id=instance.instance_id,
        output_file_path=(
            f"s3://generated_reports/{instance.organization_id}/report.json"
        ),
        output_size_bytes=len(encoded),
    )


def test_report_json_records_failure_instead_of_falling_back_to_disk() -> None:
    db, instance, _definition = _report_generation_state()
    upload = MagicMock()
    upload.save.side_effect = FileStorageError("Object storage upload failed")

    with (
        patch.object(
            ReportInstanceService,
            "_commit_and_refresh",
            side_effect=lambda _db, value: value,
        ),
        patch.object(ReportInstanceService, "_generate_payload", return_value={}),
        patch.object(
            ReportInstanceService,
            "fail_generation",
            return_value=instance,
        ) as fail,
        patch(
            "app.services.finance.rpt.report_instance.get_generated_report_upload",
            return_value=upload,
        ),
        pytest.raises(FileStorageError),
    ):
        ReportInstanceService.generate_report(
            db,
            instance.organization_id,
            instance.instance_id,
            instance.generated_by_user_id,
        )

    fail.assert_called_once_with(
        db=db,
        instance_id=instance.instance_id,
        error_message="Object storage upload failed",
    )


def test_report_data_reads_only_the_stored_object_reference() -> None:
    org_id = uuid4()
    instance = SimpleNamespace(
        instance_id=uuid4(),
        organization_id=org_id,
        status=ReportStatus.COMPLETED,
        output_file_path="s3://generated_reports/tenant/report.json",
    )
    db = MagicMock()
    db.get.return_value = instance
    storage = MagicMock()
    storage.exists.return_value = True
    storage.download.return_value = b'{"rows": [1]}'

    with patch(
        "app.services.finance.rpt.report_instance.get_storage",
        return_value=storage,
    ):
        result = ReportInstanceService.get_report_data(
            db, str(org_id), str(instance.instance_id)
        )

    assert result == {"rows": [1]}
    storage.download.assert_called_once_with("generated_reports/tenant/report.json")


def test_report_data_distinguishes_storage_failure_from_missing_output() -> None:
    org_id = uuid4()
    instance = SimpleNamespace(
        instance_id=uuid4(),
        organization_id=org_id,
        status=ReportStatus.COMPLETED,
        output_file_path="s3://generated_reports/tenant/report.json",
    )
    db = MagicMock()
    db.get.return_value = instance
    storage = MagicMock()
    storage.exists.side_effect = RuntimeError("provider unavailable")

    with (
        patch(
            "app.services.finance.rpt.report_instance.get_storage",
            return_value=storage,
        ),
        pytest.raises(HTTPException) as raised,
    ):
        ReportInstanceService.get_report_data(
            db, str(org_id), str(instance.instance_id)
        )

    assert raised.value.status_code == 503


def _document_template() -> SimpleNamespace:
    template = SimpleNamespace(
        template_id=uuid4(),
        version=1,
        header_config=None,
        footer_config=None,
    )
    template.render = MagicMock(return_value="<p>Generated</p>")
    return template


def test_generated_pdf_record_holds_the_object_key() -> None:
    org_id = uuid4()
    pdf = b"%PDF-1.7 generated"
    db = MagicMock()
    service = DocumentGeneratorService(db)
    service.get_template = MagicMock(return_value=_document_template())
    upload = MagicMock()
    upload.save.return_value = _upload_result(
        f"generated_docs/{org_id}/document.pdf", pdf
    )
    html = MagicMock()
    html.write_pdf.return_value = pdf

    with (
        patch.dict(
            sys.modules, {"weasyprint": MagicMock(HTML=MagicMock(return_value=html))}
        ),
        patch(
            "app.services.automation.document_generator.get_generated_docs_upload",
            return_value=upload,
        ),
    ):
        _bytes, record = service.generate_pdf(
            organization_id=org_id,
            template_type=TemplateType.OFFER_LETTER,
            context={},
            entity_type="JOB_OFFER",
            entity_id=uuid4(),
            document_number="OFFER/001",
            created_by=uuid4(),
            save_file=True,
            use_base_template=False,
        )

    assert record is not None
    assert record.file_path == f"generated_docs/{org_id}/document.pdf"
    upload.save.assert_called_once_with(
        pdf,
        content_type="application/pdf",
        subdirs=(str(org_id),),
        original_filename="OFFER-001.pdf",
    )


def test_generated_pdf_failure_creates_no_database_record() -> None:
    org_id = uuid4()
    db = MagicMock()
    service = DocumentGeneratorService(db)
    service.get_template = MagicMock(return_value=_document_template())
    upload = MagicMock()
    upload.save.side_effect = FileStorageError("Object storage upload failed")
    html = MagicMock()
    html.write_pdf.return_value = b"%PDF-1.7 generated"

    with (
        patch.dict(
            sys.modules, {"weasyprint": MagicMock(HTML=MagicMock(return_value=html))}
        ),
        patch(
            "app.services.automation.document_generator.get_generated_docs_upload",
            return_value=upload,
        ),
        pytest.raises(DocumentStorageError, match="temporarily unavailable"),
    ):
        service.generate_pdf(
            organization_id=org_id,
            template_type=TemplateType.OFFER_LETTER,
            context={},
            entity_type="JOB_OFFER",
            entity_id=uuid4(),
            created_by=uuid4(),
            save_file=True,
            use_base_template=False,
        )

    db.add.assert_not_called()
