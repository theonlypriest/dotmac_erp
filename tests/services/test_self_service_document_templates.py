from pathlib import Path


def test_self_service_document_form_uses_multipart_csrf_and_no_employee_id():
    template = Path("templates/people/self/document_form.html").read_text(
        encoding="utf-8"
    )

    assert 'enctype="multipart/form-data"' in template
    assert "request.state.csrf_form | safe" in template
    assert "Add another" in template
    assert "Submit all for approval" in template
    assert "`file_${index + 1}`" in template
    assert 'name="employee_id"' not in template
    assert 'name="file_path"' not in template


def test_self_service_document_listing_links_to_secure_downloads():
    template = Path("templates/people/self/documents.html").read_text(encoding="utf-8")

    assert "/people/self/documents/{{ doc.document_id }}/download" in template
    assert "/people/self/documents/pending/{{ req.request_id }}/download" in template
    assert "employee_id" not in template


def test_documents_page_loads_extended_profile_sections_inline():
    template = Path("templates/people/self/documents.html").read_text(encoding="utf-8")

    assert ">All</a>" in template
    assert 'id="extended-profile-content"' in template
    for section in ["qualifications", "certifications", "skills", "dependents"]:
        assert f'hx-get="/people/self/{section}"' in template
        assert 'hx-target="#extended-profile-content"' in template
        assert 'hx-select="#extended-profile-content"' in template
        assert 'hx-swap="outerHTML"' in template


def test_self_service_landing_only_links_to_documents_for_extended_profile():
    template = Path("templates/people/self/index.html").read_text(encoding="utf-8")

    assert 'href="/people/self/documents"' in template
    for section in ["qualifications", "certifications", "skills", "dependents"]:
        assert f'href="/people/self/{section}"' not in template
