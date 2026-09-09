"""HR Handbook Admin Web Routes.

Provides HR admin interface for managing:
- HR Policy documents
- Employee handbooks
- Acknowledgment tracking
"""

import re
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.services.people.hr.handbook_service import (
    HRDocumentFileNotFoundError,
    HRDocumentService,
    HRDocumentStorageUnavailableError,
)
from app.services.people.hr.web.handbook_web import handbook_web_service
from app.web.deps import get_db_for_org, WebAuthContext, require_hr_access

router = APIRouter(prefix="/handbook", tags=["handbook"])


# ═══════════════════════════════════════════════════════════════════════════════
# Document List & CRUD
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/", response_class=HTMLResponse)
def documents_list(
    request: Request,
    category: str | None = Query(None),
    status: str | None = Query(None),
    search: str | None = Query(None),
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """List all HR documents."""
    return handbook_web_service.documents_list_response(
        request=request,
        auth=auth,
        db=db,
        category=category,
        status=status,
        search=search,
    )


@router.get("/new", response_class=HTMLResponse)
def new_document_form(
    request: Request,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Form to create new document."""
    return handbook_web_service.document_form_response(
        request=request, auth=auth, db=db
    )


@router.post("/new")
async def create_document(
    request: Request,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Create new document with file upload."""
    return await handbook_web_service.save_document_response(
        request=request, auth=auth, db=db
    )


@router.get("/{document_id}", response_class=HTMLResponse)
def document_detail(
    request: Request,
    document_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """View document details with acknowledgment stats."""
    return handbook_web_service.document_detail_response(
        request=request, auth=auth, db=db, document_id=document_id
    )


@router.get("/{document_id}/edit", response_class=HTMLResponse)
def edit_document_form(
    request: Request,
    document_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Form to edit document."""
    return handbook_web_service.document_form_response(
        request=request, auth=auth, db=db, document_id=document_id
    )


@router.post("/{document_id}/edit")
async def update_document(
    request: Request,
    document_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Update document metadata."""
    return await handbook_web_service.save_document_response(
        request=request, auth=auth, db=db, document_id=document_id
    )


@router.post("/{document_id}/activate")
async def activate_document(
    request: Request,
    document_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Activate a document."""
    return await handbook_web_service.activate_document_response(
        request=request, auth=auth, db=db, document_id=document_id
    )


@router.post("/{document_id}/archive")
async def archive_document(
    request: Request,
    document_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Archive a document."""
    return await handbook_web_service.archive_document_response(
        request=request, auth=auth, db=db, document_id=document_id
    )


@router.get("/{document_id}/download")
def download_document(
    request: Request,
    document_id: UUID,
    auth: WebAuthContext = Depends(require_hr_access),
    db: Session = Depends(get_db_for_org),
):
    """Download document file."""
    service = HRDocumentService(db)
    document = service.get_document(auth.organization_id, document_id)
    try:
        chunks, content_type, content_length = service.stream_document(document)
    except HRDocumentFileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    except HRDocumentStorageUnavailableError as exc:
        raise HTTPException(
            status_code=503, detail="Document storage is temporarily unavailable"
        ) from exc

    safe_name = re.sub(r'[\x00-\x1f\x7f"\\]', "_", document.file_name)
    headers = {
        "Content-Disposition": (
            f"attachment; filename=\"{safe_name}\"; filename*=UTF-8''{quote(safe_name)}"
        )
    }
    if content_length is not None:
        headers["Content-Length"] = str(content_length)

    return StreamingResponse(
        chunks,
        media_type=content_type or document.content_type,
        headers=headers,
    )
