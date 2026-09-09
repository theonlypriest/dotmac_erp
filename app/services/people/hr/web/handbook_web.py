"""
HR Handbook Web Service.

Provides template response helpers for HR handbook management.
"""

import logging
from datetime import date
from uuid import UUID

from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.models.people.hr.handbook import DocumentCategory, DocumentStatus
from app.services.people.hr.handbook_service import (
    HRDocumentService,
    HRDocumentStorageUnavailableError,
    HRDocumentValidationError,
)
from app.services.upload_utils import get_env_max_bytes, read_upload_bytes
from app.templates import templates
from app.web.deps import WebAuthContext, base_context

logger = logging.getLogger(__name__)


class HRHandbookWebService:
    """Web service for HR handbook management."""

    @staticmethod
    def _require_org_id(auth: WebAuthContext) -> UUID:
        if auth.organization_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return auth.organization_id

    @staticmethod
    def _require_user_id(auth: WebAuthContext) -> UUID:
        if auth.user_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return auth.user_id

    def documents_list_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        category: str | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> HTMLResponse:
        """Render documents list page."""
        org_id = self._require_org_id(auth)
        service = HRDocumentService(db)

        # Validate enum values - fall back to None if invalid
        category_filter = None
        status_filter = None
        try:
            if category:
                category_filter = DocumentCategory(category)
        except ValueError:
            logger.warning("Invalid category filter: %s", category)
            category = None  # Reset for template

        try:
            if status:
                status_filter = DocumentStatus(status)
        except ValueError:
            logger.warning("Invalid status filter: %s", status)
            status = None  # Reset for template

        documents, total = service.list_documents(
            org_id,
            category=category_filter,
            status=status_filter,
            search=search,
            limit=100,
        )

        # Get stats for documents requiring acknowledgment (batch query - no N+1)
        docs_needing_stats = [
            doc.document_id for doc in documents if doc.requires_acknowledgment
        ]
        stats_map = (
            service.get_batch_acknowledgment_stats(org_id, docs_needing_stats)
            if docs_needing_stats
            else {}
        )

        context = base_context(request, auth, "Handbook & Policies", "handbook", db=db)
        context.update(
            {
                "documents": documents,
                "stats_map": stats_map,
                "total": total,
                "categories": list(DocumentCategory),
                "statuses": list(DocumentStatus),
                "category_filter": category,
                "status_filter": status,
                "search": search,
                "csrf_token": getattr(request.state, "csrf_token", ""),
            }
        )
        return templates.TemplateResponse("people/hr/handbook/documents.html", context)

    def document_form_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        document_id: UUID | None = None,
    ) -> HTMLResponse:
        """Render document create/edit form."""
        org_id = self._require_org_id(auth)
        service = HRDocumentService(db)
        document = None

        if document_id:
            document = service.get_document(org_id, document_id)

        context = base_context(request, auth, "Handbook Document", "handbook", db=db)
        context.update(
            {
                "document": document,
                "categories": list(DocumentCategory),
                "statuses": list(DocumentStatus),
                "csrf_token": getattr(request.state, "csrf_token", ""),
            }
        )
        return templates.TemplateResponse(
            "people/hr/handbook/document_form.html", context
        )

    async def save_document_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        *,
        document_id: UUID | None = None,
    ) -> RedirectResponse | HTMLResponse:
        """Handle document form submission."""
        org_id = self._require_org_id(auth)
        user_id = self._require_user_id(auth)
        service = HRDocumentService(db)
        form = await request.form()

        try:
            if document_id:
                # Update existing document metadata
                service.update_document(
                    org_id,
                    document_id,
                    title=str(form.get("title", "")),
                    description=str(form.get("description", "")) or None,
                    effective_date=date.fromisoformat(str(form.get("effective_date")))
                    if form.get("effective_date")
                    else None,
                    expiry_date=date.fromisoformat(str(form.get("expiry_date")))
                    if form.get("expiry_date")
                    else None,
                    requires_acknowledgment=form.get("requires_acknowledgment") == "on",
                    acknowledgment_deadline_days=int(
                        str(form.get("acknowledgment_deadline_days"))
                    )
                    if form.get("acknowledgment_deadline_days")
                    else None,
                    applies_to_all_employees=form.get("applies_to_all_employees")
                    == "on",
                    status=DocumentStatus(str(form.get("status")))
                    if form.get("status")
                    else None,
                    updated_by=user_id,
                )
                db.commit()
                return RedirectResponse(
                    url=f"/people/hr/handbook/{document_id}?saved=1",
                    status_code=303,
                )
            else:
                # Create new document with file upload
                file: UploadFile | None = form.get("file")  # type: ignore
                if not file or not file.filename:
                    raise HRDocumentValidationError("Please upload a document file")

                max_bytes = get_env_max_bytes("MAX_HR_DOC_SIZE", 10 * 1024 * 1024)
                file_content = await read_upload_bytes(
                    file,
                    max_bytes,
                    error_detail=(
                        f"File too large. Maximum size: {max_bytes // 1024 // 1024}MB"
                    ),
                )
                if len(file_content) == 0:
                    raise HRDocumentValidationError("Uploaded file is empty")

                # Save file
                file_path, file_size, content_hash = service.save_document_file(
                    org_id,
                    file.filename,
                    file_content,
                    content_type=file.content_type,
                )

                # Create document record
                document = service.create_document(
                    org_id,
                    document_code=str(form.get("document_code", "")),
                    title=str(form.get("title", "")),
                    description=str(form.get("description", "")) or None,
                    category=DocumentCategory(str(form.get("category", "POLICY"))),
                    file_path=file_path,
                    file_name=file.filename,
                    file_size_bytes=file_size,
                    content_type=file.content_type or "application/pdf",
                    content_hash=content_hash,
                    effective_date=date.fromisoformat(str(form.get("effective_date")))
                    if form.get("effective_date")
                    else date.today(),
                    requires_acknowledgment=form.get("requires_acknowledgment") == "on",
                    acknowledgment_deadline_days=int(
                        str(form.get("acknowledgment_deadline_days"))
                    )
                    if form.get("acknowledgment_deadline_days")
                    else None,
                    status=DocumentStatus(str(form.get("status", "DRAFT"))),
                    created_by=user_id,
                )
                db.commit()

                return RedirectResponse(
                    url=f"/people/hr/handbook/{document.document_id}?saved=1",
                    status_code=303,
                )

        except (HRDocumentValidationError, HRDocumentStorageUnavailableError) as exc:
            storage_unavailable = isinstance(exc, HRDocumentStorageUnavailableError)
            document_opt = (
                service.get_document(org_id, document_id) if document_id else None
            )
            context = base_context(
                request, auth, "Handbook Document", "handbook", db=db
            )
            context.update(
                {
                    "document": document_opt,
                    "categories": list(DocumentCategory),
                    "statuses": list(DocumentStatus),
                    "error": str(exc),
                    "csrf_token": getattr(request.state, "csrf_token", ""),
                }
            )
            return templates.TemplateResponse(
                "people/hr/handbook/document_form.html",
                context,
                status_code=503 if storage_unavailable else 400,
            )

    def document_detail_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        document_id: UUID,
    ) -> HTMLResponse:
        """Render document detail page with acknowledgment stats."""
        org_id = self._require_org_id(auth)
        service = HRDocumentService(db)

        document = service.get_document(org_id, document_id)
        stats = service.get_acknowledgment_stats(org_id, document_id)
        acknowledgments, ack_total = service.list_document_acknowledgments(
            org_id, document_id, limit=50
        )
        pending_employees = service.get_pending_employees(org_id, document_id)[
            :20
        ]  # Limit to first 20

        context = base_context(request, auth, "Handbook Document", "handbook", db=db)
        context.update(
            {
                "document": document,
                "stats": stats,
                "acknowledgments": acknowledgments,
                "pending_employees": pending_employees,
                "csrf_token": getattr(request.state, "csrf_token", ""),
            }
        )
        return templates.TemplateResponse(
            "people/hr/handbook/document_detail.html", context
        )

    async def activate_document_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        document_id: UUID,
    ) -> RedirectResponse:
        """Activate a document."""
        org_id = self._require_org_id(auth)
        user_id = self._require_user_id(auth)
        service = HRDocumentService(db)
        service.activate_document(org_id, document_id, user_id)
        db.commit()
        return RedirectResponse(
            url=f"/people/hr/handbook/{document_id}?saved=1",
            status_code=303,
        )

    async def archive_document_response(
        self,
        request: Request,
        auth: WebAuthContext,
        db: Session,
        document_id: UUID,
    ) -> RedirectResponse:
        """Archive a document."""
        org_id = self._require_org_id(auth)
        user_id = self._require_user_id(auth)
        service = HRDocumentService(db)
        service.archive_document(org_id, document_id, user_id)
        db.commit()
        return RedirectResponse(
            url=f"/people/hr/handbook/{document_id}?saved=1",
            status_code=303,
        )


# Singleton instance
handbook_web_service = HRHandbookWebService()
