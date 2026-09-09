import re
from pathlib import Path


BREADCRUMB_BLOCK = re.compile(
    r"{% block breadcrumbs %}(?P<body>.*?){% endblock %}",
    re.DOTALL,
)
SELF_SERVICE_LINK = re.compile(
    r"""<a\s+[^>]*href=["'](?P<href>[^"']+)["'][^>]*>\s*Self Service\s*</a>""",
    re.IGNORECASE,
)


def test_self_service_breadcrumb_links_return_to_self_service_landing():
    for template_path in Path("templates/people/self").glob("*.html"):
        template = template_path.read_text(encoding="utf-8")
        match = BREADCRUMB_BLOCK.search(template)
        if not match:
            continue

        for link in SELF_SERVICE_LINK.finditer(match.group("body")):
            assert link.group("href") == "/people/self", template_path
