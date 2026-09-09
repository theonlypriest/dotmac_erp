from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_new_self_service_expense_item_template_has_blank_required_category_option():
    template = (REPO_ROOT / "templates/people/self/expenses.html").read_text()

    # Robust against attribute reordering — assert on the structural pieces only.
    assert 'name="category_id___KEY__"' in template
    assert 'class="form-select w-full" required data-item-category' in template
    assert '<option value="" selected disabled>Select category...</option>' in template


def test_edit_self_service_expense_item_template_has_blank_required_category_option():
    template = (REPO_ROOT / "templates/people/self/expense_claim_edit.html").read_text()

    # Robust against attribute reordering — assert on the structural pieces only.
    assert 'name="category_id___KEY__"' in template
    assert 'class="form-select w-full" required' in template
    assert '<option value="" selected disabled>Select category...</option>' in template


def test_self_service_expense_descriptions_show_and_enforce_the_database_limit():
    create_template = (REPO_ROOT / "templates/people/self/expenses.html").read_text()
    edit_template = (
        REPO_ROOT / "templates/people/self/expense_claim_edit.html"
    ).read_text()

    for template in (create_template, edit_template):
        assert 'maxlength="500"' in template
        assert "data-expense-description" in template
        assert "data-description-count" in template
        assert "/ 500 characters" in template


def test_approver_expense_description_corrections_show_and_enforce_limit():
    template = (REPO_ROOT / "templates/expense/claim_detail.html").read_text()

    assert 'maxlength="500"' in template
    assert 'aria-live="polite"' in template
    assert "descriptionLength" in template
    assert "/ 500 characters" in template
