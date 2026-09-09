"""
HR Core Models.

This module contains all models for the HR Core functionality:
- Department: Organizational units
- Designation: Job titles/positions
- EmploymentType: Types of employment (full-time, part-time, etc.)
- EmployeeGrade: Salary grades/bands
- Employee: Central employee entity linking Person to HR
"""

from app.models.people.hr.checklist_template import (
    AssigneeRole,
    ChecklistTemplate,
    ChecklistTemplateItem,
    ChecklistTemplateType,
    OnboardingCategory,
)
from app.models.people.hr.department import Department
from app.models.people.hr.designation import Designation
from app.models.people.hr.employee import Employee, EmployeeStatus, Gender
from app.models.people.hr.staff_access_projection import (
    StaffAccountStatusProjection,
    StaffAccountStatusState,
    StaffLeaveAccessRestriction,
    StaffLeaveRestrictionStatus,
)
from app.models.people.hr.employee_extended import (
    DocumentType,
    EmployeeCertification,
    EmployeeDependent,
    EmployeeDocument,
    EmployeeQualification,
    EmployeeSkill,
    QualificationType,
    RelationshipType,
    Skill,
    SkillCategory,
)
from app.models.people.hr.info_change_request import (
    EmployeeInfoChangeBatch,
    EmployeeInfoChangeRequest,
    InfoChangeOperation,
    InfoChangeStatus,
    InfoChangeType,
)
from app.models.people.hr.employee_grade import EmployeeGrade
from app.models.people.hr.employment_type import EmploymentType
from app.models.people.hr.position import Position, PositionVacancyRoutingPolicy
from app.models.people.hr.position_assignment import (
    PositionAssignment,
    PositionAssignmentType,
)
from app.models.people.hr.handbook import (
    DocumentCategory,
    DocumentStatus,
    HRDocument,
    HRDocumentAcknowledgment,
)
from app.models.people.hr.job_description import (
    Competency,
    CompetencyCategory,
    JobDescription,
    JobDescriptionCompetency,
    JobDescriptionStatus,
)
from app.models.people.hr.succession import (
    ImpactLevel,
    ReadinessLevel,
    RiskLevel,
    SuccessionCandidate,
    SuccessionPlan,
    SuccessionPlanStatus,
)
from app.models.people.hr.survey import (
    QuestionType,
    Survey,
    SurveyAnswer,
    SurveyQuestion,
    SurveyResponse,
    SurveyStatus,
    SurveyType,
    TargetAudience,
)
from app.models.people.hr.grievance import (
    Grievance,
    GrievanceCategory,
    GrievanceSeverity,
    GrievanceStatus,
)
from app.models.people.hr.lifecycle import (
    ActivityStatus,
    BoardingStatus,
    EmployeeOnboarding,
    EmployeeOnboardingActivity,
    EmployeePromotion,
    EmployeePromotionDetail,
    EmployeeSeparation,
    EmployeeSeparationActivity,
    EmployeeTransfer,
    EmployeeTransferDetail,
    SeparationType,
)

from app.models.people.hr.clearance_checklist import ClearanceCategory, ClearanceItem
from app.models.people.hr.employment_contract import (
    ContractStatus,
    ContractType,
    EmploymentContract,
)
from app.models.people.hr.exit_interview import (
    ExitInterview,
    InterviewStatus,
    OverallExperience,
    ReasonForLeaving,
)
from app.models.people.hr.salary_review import (
    ReviewType,
    SalaryReview,
    SalaryReviewStatus,
)

__all__ = [
    "Department",
    "Designation",
    "Employee",
    "EmployeeGrade",
    "EmployeeStatus",
    "StaffAccountStatusProjection",
    "StaffAccountStatusState",
    "StaffLeaveAccessRestriction",
    "StaffLeaveRestrictionStatus",
    "EmploymentType",
    "Gender",
    "Position",
    "PositionVacancyRoutingPolicy",
    "PositionAssignment",
    "PositionAssignmentType",
    # Lifecycle enums and models
    "ActivityStatus",
    "BoardingStatus",
    "SeparationType",
    "EmployeeOnboarding",
    "EmployeeOnboardingActivity",
    "EmployeeSeparation",
    "EmployeeSeparationActivity",
    "EmployeePromotion",
    "EmployeePromotionDetail",
    "EmployeeTransfer",
    "EmployeeTransferDetail",
    # Checklist templates
    "AssigneeRole",
    "ChecklistTemplate",
    "ChecklistTemplateItem",
    "ChecklistTemplateType",
    "OnboardingCategory",
    # Employee extended data
    "DocumentType",
    "QualificationType",
    "RelationshipType",
    "SkillCategory",
    "EmployeeDocument",
    "EmployeeQualification",
    "EmployeeCertification",
    "EmployeeDependent",
    "Skill",
    "EmployeeSkill",
    "EmployeeInfoChangeBatch",
    "EmployeeInfoChangeRequest",
    "InfoChangeOperation",
    "InfoChangeStatus",
    "InfoChangeType",
    # Job descriptions and competencies
    "CompetencyCategory",
    "JobDescriptionStatus",
    "Competency",
    "JobDescription",
    "JobDescriptionCompetency",
    # Grievance
    "Grievance",
    "GrievanceCategory",
    "GrievanceSeverity",
    "GrievanceStatus",
    # Salary Review
    "SalaryReview",
    "SalaryReviewStatus",
    "ReviewType",
    # HR Documents / Handbook
    "DocumentCategory",
    "DocumentStatus",
    "HRDocument",
    "HRDocumentAcknowledgment",
    # Survey
    "Survey",
    "SurveyQuestion",
    "SurveyResponse",
    "SurveyAnswer",
    "SurveyType",
    "SurveyStatus",
    "TargetAudience",
    "QuestionType",
    # Succession Planning
    "SuccessionPlan",
    "SuccessionCandidate",
    "SuccessionPlanStatus",
    "ReadinessLevel",
    "RiskLevel",
    "ImpactLevel",
    # Employment Contracts
    "EmploymentContract",
    "ContractType",
    "ContractStatus",
    # Exit Interview
    "ExitInterview",
    "InterviewStatus",
    "OverallExperience",
    "ReasonForLeaving",
    # Clearance Checklist
    "ClearanceItem",
    "ClearanceCategory",
]
