from pathlib import Path


def test_extended_profile_templates_include_csrf_and_no_employee_id_field():
    for name in [
        "qualifications.html",
        "certifications.html",
        "skills.html",
        "dependents.html",
    ]:
        template = Path(f"templates/people/self/{name}").read_text(encoding="utf-8")
        assert "request.state.csrf_form | safe" in template
        assert 'name="employee_id"' not in template
        assert "employee_id" not in template.split("<form", 1)[1]
        assert "Add another" in template
        assert "Submit all for approval" in template
        assert "x-data" in template


def test_extended_profile_templates_do_not_offer_delete_actions():
    for name in [
        "qualifications.html",
        "certifications.html",
        "skills.html",
        "dependents.html",
    ]:
        template = Path(f"templates/people/self/{name}").read_text(encoding="utf-8")
        assert "Delete" not in template
        assert "/delete" not in template


def test_extended_profile_templates_render_repeatable_row_headings_and_remove():
    for name in [
        "qualifications.html",
        "certifications.html",
        "skills.html",
        "dependents.html",
    ]:
        template = Path(f"templates/people/self/{name}").read_text(encoding="utf-8")
        assert "Remove" in template
        assert "${index + 1}" in template


def test_extended_profile_templates_support_inline_document_page_workflow():
    for section in [
        "qualifications",
        "certifications",
        "skills",
        "dependents",
    ]:
        template = Path(f"templates/people/self/{section}.html").read_text(
            encoding="utf-8"
        )
        assert 'id="extended-profile-content"' in template
        assert 'hx-boost="true"' in template
        assert 'hx-target="#extended-profile-content"' in template
        assert 'hx-select="#extended-profile-content"' in template
        assert 'hx-swap="outerHTML"' in template
        assert 'hx-push-url="false"' in template
        assert f'action="/people/self/{section}"' in template
