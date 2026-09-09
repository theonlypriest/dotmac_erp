from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch
from uuid import uuid4

from app.services.people.payroll.web.slip_web import SlipWebService


def test_post_slip_response_creates_notification_and_queues_email():
    org_id = uuid4()
    user_id = uuid4()
    slip_id = uuid4()
    employee = SimpleNamespace(employee_id=uuid4(), person_id=uuid4())
    slip = SimpleNamespace(
        slip_id=slip_id,
        organization_id=org_id,
        employee=employee,
    )
    auth = SimpleNamespace(organization_id=str(org_id), user_id=str(user_id))
    db = MagicMock()
    db.get.return_value = slip

    with (
        patch(
            "app.services.people.payroll.web.slip_web.PayrollGLAdapter.post_salary_slip"
        ) as mock_post,
        patch(
            "app.services.people.payroll.payroll_notifications.PayrollNotificationService"
        ) as mock_service_cls,
    ):
        mock_service = mock_service_cls.return_value

        response = SlipWebService().post_slip_response(auth, db, str(slip_id))

        assert response.status_code == 303
        assert response.headers["location"] == (
            f"/people/payroll/slips/{slip_id}?saved=1"
        )
        mock_post.assert_called_once()
        db.get.assert_called_once_with(ANY, slip_id)
        mock_service.notify_payslip_posted.assert_called_once_with(
            slip,
            employee,
            queue_email=True,
        )
        db.commit.assert_called_once()


def test_list_slips_filters_by_employment_type_within_organization():
    org_id = uuid4()
    employment_type_id = uuid4()
    auth = SimpleNamespace(organization_id=str(org_id))
    request = SimpleNamespace(query_params={})
    db = MagicMock()
    page_result = SimpleNamespace(items=[], total=0, total_pages=1)
    db.scalar.return_value = 0
    db.scalars.return_value.all.return_value = []

    with (
        patch(
            "app.services.people.payroll.web.slip_web.paginate",
            return_value=page_result,
        ) as mock_paginate,
        patch("app.services.people.payroll.web.slip_web.base_context", return_value={}),
        patch("app.services.people.payroll.web.slip_web.templates.TemplateResponse"),
        patch(
            "app.services.people.payroll.web.slip_web.EmploymentTypeService"
        ) as employment_type_service,
    ):
        employment_type_service.return_value.iter_all.return_value = ()
        SlipWebService().list_slips_response(
            request,
            auth,
            db,
            employment_type_id=str(employment_type_id),
        )

    statement = mock_paginate.call_args.args[1]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "hr.employee.employment_type_id" in compiled
    assert str(employment_type_id) in compiled
    assert str(org_id) in compiled
    employment_type_service.assert_called_once_with(db, org_id)
    employment_type_service.return_value.iter_all.assert_called_once_with(active=True)
