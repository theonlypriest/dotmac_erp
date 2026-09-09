from pathlib import Path


TEMPLATE = Path("templates/people/hr/employees.html")


def _block(source: str, name: str) -> str:
    start = source.index(f"{{% block {name} %}}")
    end = source.index("{% endblock %}", start)
    return source[start:end]


def test_employee_export_action_lives_in_filter_panel_not_header_actions():
    source = TEMPLATE.read_text(encoding="utf-8")
    header_actions = _block(source, "header_actions")
    filters_panel = source.split("{# Compact Filters #}", 1)[1].split(
        "{% endcall %}", 1
    )[0]

    assert "Org Chart" in header_actions
    assert "Create Employee" in header_actions
    assert "Export Employees" not in header_actions

    assert "Export Employees" in filters_panel
    assert "open-employee-export" in filters_panel
    assert "col-span-full flex justify-end" in filters_panel
