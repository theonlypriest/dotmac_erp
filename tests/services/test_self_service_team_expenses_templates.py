from pathlib import Path


TEMPLATE = Path("templates/people/self/team_expenses.html")


def test_team_expenses_page_title_and_filters():
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "{% block page_title %}Team Expenses{% endblock %}" in template
    assert "My Expense Approvals" in template
    assert 'base_url="/people/self/team/expenses"' in template
    assert 'name="decision"' in template
    assert 'name="status"' in template
    assert 'name="employee_id"' in template
    assert "date_range=true" in template
    assert "Pending Claims" in template
    assert "Paid Claims" in template
