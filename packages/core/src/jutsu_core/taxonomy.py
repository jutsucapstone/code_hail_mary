"""The role taxonomy — practice, title, normalized level, and the JUTSU role code.

Two source documents, two different things, and the whole point of this module is that
they stay two things (see `docs/adr/0015-role-taxonomy.md`):

* **`Deloitte_USI_Role_Taxonomy.pdf`** gives the *business* dimensions. A practice runs
  its own vocabulary — Audit says "Audit Senior Assistant", Tax says "Tax Consultant I",
  Risk says "Assistant Manager" — so a title alone cannot be compared across practices.
  Every title therefore carries a separate **normalized level**, and it is the level that
  makes "find the Senior-Consultant-equivalent people on Project X" answerable at all.
* **`mindmap of role codes.pdf`** gives the *platform* dimension: eleven JUTSU role codes
  with tiers, four of which are governance seats.

**A role code is not a `Role`.** `rbac.py` decides what a caller may *do*; nothing here
does. `HRA` resembles `Role.HR_ADMIN` and `ITA` resembles `Role.IT_ADMIN`, and that
resemblance is a trap: these codes are organisational standing recorded on a profile,
and if one ever conferred a permission because its string looked like a role key, the
control plane would have been widened by a piece of HR data. `test_taxonomy.py` asserts
the two vocabularies share no value, so the confusion cannot compile.

This module is the **authoring copy**, exactly as `rbac.py` is for roles: migration 0018
seeds these same rows into Postgres and the application reads the database, never this.
`test_taxonomy_catalogue.py` asserts the two are identical so they cannot drift.

Ambiguity is preserved rather than resolved. Where the source maps one title to two
levels — "Audit Senior → Consultant / Senior Consultant" — both are stored and an
assignment must name which one applies. `LEVELS_FOR_TITLE` is therefore a tuple, never a
scalar, and the first entry is the source document's own first-listed level: that
ordering is the documented tie-break, not a guess this module made.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

__all__ = [
    "CODES",
    "DISCIPLINES",
    "LEVELS",
    "LEVEL_TO_CODE",
    "PRACTICES",
    "PRIVILEGED_CODES",
    "TITLES",
    "Discipline",
    "MappingStatus",
    "Practice",
    "RoleCode",
    "RoleCodeEntry",
    "RoleLevel",
    "RoleLevelEntry",
    "RoleTitleEntry",
    "code_for_level",
    "is_privileged_code",
    "levels_for_title",
]


# --------------------------------------------------------------------------- practices


class Practice(StrEnum):
    """A business line. The top of the taxonomy and the first thing an admin picks."""

    TECHNOLOGY = "technology"
    QUALITY_ASSURANCE = "quality_assurance"
    AUDIT_ASSURANCE = "audit_assurance"
    TAX_LEGAL = "tax_legal"
    RISK_FINANCIAL_ADVISORY = "risk_financial_advisory"


PRACTICES: Final[MappingProxyType[Practice, str]] = MappingProxyType(
    {
        Practice.TECHNOLOGY: "Technology",
        Practice.QUALITY_ASSURANCE: "Quality Assurance / Testing",
        Practice.AUDIT_ASSURANCE: "Audit & Assurance",
        Practice.TAX_LEGAL: "Tax & Legal Services",
        Practice.RISK_FINANCIAL_ADVISORY: "Risk & Financial Advisory",
    }
)


class Discipline(StrEnum):
    """A sub-function inside a practice.

    Only Technology has them in the source document, and inventing one for the other four
    practices purely for symmetry would put a name in the catalogue that no source
    supports. A title's discipline is therefore optional.
    """

    SOFTWARE_ENGINEERING = "software_engineering"
    DATA_ANALYTICS = "data_analytics"
    CLOUD_DEVOPS = "cloud_devops"


DISCIPLINES: Final[MappingProxyType[Discipline, tuple[Practice, str]]] = MappingProxyType(
    {
        Discipline.SOFTWARE_ENGINEERING: (Practice.TECHNOLOGY, "Software Engineering"),
        Discipline.DATA_ANALYTICS: (Practice.TECHNOLOGY, "Data & Analytics"),
        Discipline.CLOUD_DEVOPS: (Practice.TECHNOLOGY, "Cloud & DevOps"),
    }
)


# ------------------------------------------------------------------------------ levels


class RoleLevel(StrEnum):
    """Normalized seniority, independent of practice. The cross-practice comparator."""

    ANALYST = "analyst"
    BTA = "bta"
    SENIOR_ANALYST = "senior_analyst"
    CONSULTANT = "consultant"
    CONSULTANT_SENIOR = "consultant_senior"
    SENIOR_CONSULTANT = "senior_consultant"
    ASSISTANT_MANAGER = "assistant_manager"
    MANAGER = "manager"
    SENIOR_MANAGER = "senior_manager"
    SPECIALIST_LEADER = "specialist_leader"
    DIRECTOR = "director"
    PARTNER = "partner"


@dataclass(frozen=True, slots=True)
class RoleLevelEntry:
    display_name: str
    #: Spaced, and deliberately NOT unique — the same reasoning as `ROLE_RANKS` in
    #: `rbac.py`. Equal ranks are genuine peers: the source pairs "Senior Manager /
    #: Specialist Leader" as one cell, and "Analyst / BTA" as another, so forcing a
    #: total order would invent seniority the document does not claim. The gaps leave
    #: room to insert a level without renumbering.
    rank: int
    description: str


LEVELS: Final[MappingProxyType[RoleLevel, RoleLevelEntry]] = MappingProxyType(
    {
        RoleLevel.ANALYST: RoleLevelEntry("Analyst", 10, "Entry-grade delivery and research."),
        RoleLevel.BTA: RoleLevelEntry(
            "BTA", 10, "Business Technology Analyst; entry-grade peer of Analyst."
        ),
        RoleLevel.SENIOR_ANALYST: RoleLevelEntry(
            "Senior Analyst", 20, "Analyst-grade with ownership of a workstream."
        ),
        RoleLevel.CONSULTANT: RoleLevelEntry(
            "Consultant", 30, "Independent delivery against a defined scope."
        ),
        RoleLevel.CONSULTANT_SENIOR: RoleLevelEntry(
            "Consultant (senior)",
            35,
            "The upper Consultant band; Tax names it explicitly as Tax Consultant II.",
        ),
        RoleLevel.SENIOR_CONSULTANT: RoleLevelEntry(
            "Senior Consultant", 40, "Technical lead and senior operational delivery."
        ),
        RoleLevel.ASSISTANT_MANAGER: RoleLevelEntry(
            "Assistant Manager", 50, "Junior management; named explicitly on the Risk ladder."
        ),
        RoleLevel.MANAGER: RoleLevelEntry(
            "Manager", 60, "Project leadership and delivery oversight."
        ),
        RoleLevel.SENIOR_MANAGER: RoleLevelEntry(
            "Senior Manager", 70, "Portfolio and multi-project oversight."
        ),
        RoleLevel.SPECIALIST_LEADER: RoleLevelEntry(
            "Specialist Leader",
            70,
            "Deep-specialist track; the source pairs it with Senior Manager.",
        ),
        RoleLevel.DIRECTOR: RoleLevelEntry(
            "Director", 80, "Named on the extended Risk Advisory ladder, below Partner."
        ),
        RoleLevel.PARTNER: RoleLevelEntry("Partner", 90, "Practice leadership and ownership."),
    }
)


# ------------------------------------------------------------------------------ titles


@dataclass(frozen=True, slots=True)
class RoleTitleEntry:
    display_name: str
    practice: Practice
    discipline: Discipline | None
    #: Every level the source admits for this title, in the source's own order.
    #:
    #: A tuple of two is the document being genuinely ambiguous ("Consultant / Senior
    #: Consultant"), not this catalogue hedging. Assignment must name one of these; the
    #: first is the documented default where a single value is unavoidable.
    levels: tuple[RoleLevel, ...]


def _t(
    display: str,
    practice: Practice,
    discipline: Discipline | None,
    *levels: RoleLevel,
) -> RoleTitleEntry:
    return RoleTitleEntry(display, practice, discipline, levels)


_TECH = Practice.TECHNOLOGY
_SWE = Discipline.SOFTWARE_ENGINEERING
_DATA = Discipline.DATA_ANALYTICS
_CLOUD = Discipline.CLOUD_DEVOPS
_QA = Practice.QUALITY_ASSURANCE
_AUDIT = Practice.AUDIT_ASSURANCE
_TAX = Practice.TAX_LEGAL
_RISK = Practice.RISK_FINANCIAL_ADVISORY

TITLES: Final[MappingProxyType[str, RoleTitleEntry]] = MappingProxyType(
    {
        # -- Technology / Software Engineering
        "associate_software_engineer": _t(
            "Associate Software Engineer", _TECH, _SWE, RoleLevel.ANALYST, RoleLevel.BTA
        ),
        "software_engineer": _t("Software Engineer", _TECH, _SWE, RoleLevel.CONSULTANT),
        "senior_software_engineer": _t(
            "Senior Software Engineer", _TECH, _SWE, RoleLevel.SENIOR_CONSULTANT
        ),
        "engineering_lead_manager": _t(
            "Engineering Lead / Manager", _TECH, _SWE, RoleLevel.MANAGER
        ),
        "principal_engineer_architect": _t(
            "Principal Engineer / Architect", _TECH, _SWE, RoleLevel.SENIOR_MANAGER
        ),
        # -- Technology / Data & Analytics
        "data_analyst": _t("Data Analyst", _TECH, _DATA, RoleLevel.ANALYST),
        "data_engineer": _t("Data Engineer", _TECH, _DATA, RoleLevel.CONSULTANT),
        "senior_data_scientist_engineer": _t(
            "Senior Data Scientist / Engineer", _TECH, _DATA, RoleLevel.SENIOR_CONSULTANT
        ),
        "analytics_manager": _t("Analytics Manager", _TECH, _DATA, RoleLevel.MANAGER),
        "data_ai_architect": _t(
            "Data/AI Architect",
            _TECH,
            _DATA,
            RoleLevel.SENIOR_MANAGER,
            RoleLevel.SPECIALIST_LEADER,
        ),
        # -- Technology / Cloud & DevOps
        "cloud_associate": _t("Cloud Associate", _TECH, _CLOUD, RoleLevel.ANALYST),
        "devops_engineer": _t("DevOps Engineer", _TECH, _CLOUD, RoleLevel.CONSULTANT),
        "senior_cloud_engineer": _t(
            "Senior Cloud Engineer", _TECH, _CLOUD, RoleLevel.SENIOR_CONSULTANT
        ),
        "cloud_solutions_manager": _t("Cloud Solutions Manager", _TECH, _CLOUD, RoleLevel.MANAGER),
        "cloud_architect": _t(
            "Cloud Architect",
            _TECH,
            _CLOUD,
            RoleLevel.SENIOR_MANAGER,
            RoleLevel.SPECIALIST_LEADER,
        ),
        # -- Quality Assurance / Testing
        "qa_analyst": _t("QA Analyst", _QA, None, RoleLevel.ANALYST),
        "qa_engineer": _t("QA Engineer", _QA, None, RoleLevel.CONSULTANT),
        "senior_qa_engineer_automation_lead": _t(
            "Senior QA Engineer / Automation Lead", _QA, None, RoleLevel.SENIOR_CONSULTANT
        ),
        "qa_manager": _t("QA Manager", _QA, None, RoleLevel.MANAGER),
        # -- Audit & Assurance
        "audit_assistant_analyst": _t("Audit Assistant / Analyst", _AUDIT, None, RoleLevel.ANALYST),
        "audit_senior_assistant": _t(
            "Audit Senior Assistant", _AUDIT, None, RoleLevel.SENIOR_ANALYST
        ),
        "audit_senior": _t(
            "Audit Senior (Audit In-Charge)",
            _AUDIT,
            None,
            RoleLevel.CONSULTANT,
            RoleLevel.SENIOR_CONSULTANT,
        ),
        "audit_manager": _t("Audit Manager", _AUDIT, None, RoleLevel.MANAGER),
        "audit_senior_manager": _t("Audit Senior Manager", _AUDIT, None, RoleLevel.SENIOR_MANAGER),
        "partner_managing_director": _t(
            "Partner / Managing Director", _AUDIT, None, RoleLevel.PARTNER
        ),
        # -- Tax & Legal Services
        "tax_analyst_associate": _t(
            "Tax Analyst / Associate Analyst", _TAX, None, RoleLevel.ANALYST
        ),
        "tax_consultant_i": _t("Tax Consultant I", _TAX, None, RoleLevel.CONSULTANT),
        "tax_consultant_ii": _t("Tax Consultant II", _TAX, None, RoleLevel.CONSULTANT_SENIOR),
        "senior_tax_consultant": _t(
            "Senior Tax Consultant", _TAX, None, RoleLevel.SENIOR_CONSULTANT
        ),
        "tax_manager": _t("Tax Manager", _TAX, None, RoleLevel.MANAGER),
        "tax_senior_manager": _t("Tax Senior Manager", _TAX, None, RoleLevel.SENIOR_MANAGER),
        # -- Risk & Financial Advisory (incl. Cyber)
        "risk_analyst": _t("Risk Analyst", _RISK, None, RoleLevel.ANALYST),
        "senior_risk_analyst": _t("Senior Risk Analyst", _RISK, None, RoleLevel.SENIOR_ANALYST),
        "risk_consultant": _t("Risk Consultant", _RISK, None, RoleLevel.CONSULTANT),
        "assistant_manager_risk_advisory": _t(
            "Assistant Manager, Risk Advisory", _RISK, None, RoleLevel.ASSISTANT_MANAGER
        ),
        "cyber_security_analyst": _t(
            "Cyber Security Analyst", _RISK, None, RoleLevel.ANALYST, RoleLevel.CONSULTANT
        ),
        "cyber_risk_consultant": _t(
            "Cyber Risk Consultant",
            _RISK,
            None,
            RoleLevel.CONSULTANT,
            RoleLevel.SENIOR_CONSULTANT,
        ),
        "risk_advisory_manager": _t("Risk Advisory Manager", _RISK, None, RoleLevel.MANAGER),
        "financial_advisory_analyst": _t(
            "Financial Advisory Analyst (M&A/Valuations)", _RISK, None, RoleLevel.ANALYST
        ),
        "financial_advisory_senior_consultant": _t(
            "Financial Advisory Senior Consultant", _RISK, None, RoleLevel.SENIOR_CONSULTANT
        ),
        "risk_advisory_director": _t("Risk Advisory Director", _RISK, None, RoleLevel.DIRECTOR),
    }
)


def levels_for_title(title_key: str) -> tuple[RoleLevel, ...]:
    """Every level the source admits for a title. Empty for an unknown key."""
    entry = TITLES.get(title_key)
    return entry.levels if entry is not None else ()


# ------------------------------------------------------------------------- role codes


class RoleCode(StrEnum):
    """A JUTSU platform role code. Standing on the org chart, never a permission."""

    CHM = "CHM"
    CEO = "CEO"
    ITA = "ITA"
    HRA = "HRA"
    PTR = "PTR"
    SMR = "SMR"
    MGR = "MGR"
    AMR = "AMR"
    SCN = "SCN"
    CON = "CON"
    ANS = "ANS"


class CodeCategory(StrEnum):
    EXECUTIVE = "executive"
    EXECUTION = "execution"


@dataclass(frozen=True, slots=True)
class RoleCodeEntry:
    display_name: str
    #: `T8`..`T1` in the source, stored as the integer so ordering is arithmetic.
    tier: int
    category: CodeCategory
    description: str
    #: A governance seat. These four are never derived from business seniority and their
    #: assignment carries an extra server-side check — see `roles.assign_role_code`.
    privileged: bool


CODES: Final[MappingProxyType[RoleCode, RoleCodeEntry]] = MappingProxyType(
    {
        RoleCode.CHM: RoleCodeEntry(
            "Chairman / Superadmin",
            8,
            CodeCategory.EXECUTIVE,
            "The ultimate global controller. Possesses absolute root access to everything.",
            True,
        ),
        RoleCode.CEO: RoleCodeEntry(
            "Chief Executive Officer / Admin",
            7,
            CodeCategory.EXECUTIVE,
            "The primary operational controller acting directly underneath the Chairman.",
            True,
        ),
        RoleCode.ITA: RoleCodeEntry(
            "IT Admin Controller",
            6,
            CodeCategory.EXECUTIVE,
            "Controls all security, tooling and application provisioning. Direct report to CEO.",
            True,
        ),
        RoleCode.HRA: RoleCodeEntry(
            "HR Admin Controller",
            6,
            CodeCategory.EXECUTIVE,
            "Controls personnel mapping, knowledge-transfer matrices and role data. "
            "Direct report to CEO.",
            True,
        ),
        RoleCode.PTR: RoleCodeEntry(
            "Partner",
            6,
            CodeCategory.EXECUTION,
            "Practice leadership and portfolio execution.",
            False,
        ),
        RoleCode.SMR: RoleCodeEntry(
            "Senior Manager",
            5,
            CodeCategory.EXECUTION,
            "Portfolio and multi-project oversight.",
            False,
        ),
        RoleCode.MGR: RoleCodeEntry(
            "Manager",
            4,
            CodeCategory.EXECUTION,
            "Primary project leadership and client delivery oversight.",
            False,
        ),
        RoleCode.AMR: RoleCodeEntry(
            "Assistant Manager",
            3,
            CodeCategory.EXECUTION,
            "Specialised operations and junior management.",
            False,
        ),
        RoleCode.SCN: RoleCodeEntry(
            "Senior Consultant",
            2,
            CodeCategory.EXECUTION,
            "Senior operational delivery and technical lead.",
            False,
        ),
        RoleCode.CON: RoleCodeEntry(
            "Consultant",
            1,
            CodeCategory.EXECUTION,
            "Core execution and independent delivery.",
            False,
        ),
        RoleCode.ANS: RoleCodeEntry(
            "Analyst",
            1,
            CodeCategory.EXECUTION,
            "Associate delivery, research and technical support.",
            False,
        ),
    }
)

#: The four governance seats. Assigning one needs more than the ordinary permission.
PRIVILEGED_CODES: Final[frozenset[RoleCode]] = frozenset(
    code for code, entry in CODES.items() if entry.privileged
)


def is_privileged_code(code: RoleCode) -> bool:
    return code in PRIVILEGED_CODES


# ---------------------------------------------------------------------- level → code

#: Mapping **policy**, from normalized business seniority to the platform code.
#:
#: It is a policy and not an identity, which is why it lives in its own table rather than
#: as a column on either catalogue: the two vocabularies come from different documents
#: and either can move without the other. Three things it deliberately does NOT do:
#:
#: * **`DIRECTOR` has no code.** The JUTSU catalogue jumps SMR (T5) → PTR (T6) with
#:   nothing between, and Director sits between Senior Manager and Partner on the Risk
#:   ladder. Mapping it to either would invent a promotion or a demotion, so it maps to
#:   nothing and an admin assigns the code explicitly. Absent, not guessed.
#: * **No level maps to CHM, CEO, ITA or HRA.** Governance seats are never implied by
#:   business seniority — a Manager is not an IT Admin Controller because both are senior.
#: * **It never overwrites.** Nothing calls this to mutate a stored code; it is a
#:   *suggestion* offered to the admin UI and recorded as such.
LEVEL_TO_CODE: Final[MappingProxyType[RoleLevel, RoleCode | None]] = MappingProxyType(
    {
        RoleLevel.ANALYST: RoleCode.ANS,
        RoleLevel.BTA: RoleCode.ANS,
        # Still analyst-grade: the Risk ladder places Senior Analyst below Consultant,
        # and ANS/CON are both T1, so the analyst code is the honest side of the pair.
        RoleLevel.SENIOR_ANALYST: RoleCode.ANS,
        RoleLevel.CONSULTANT: RoleCode.CON,
        RoleLevel.CONSULTANT_SENIOR: RoleCode.CON,
        RoleLevel.SENIOR_CONSULTANT: RoleCode.SCN,
        RoleLevel.ASSISTANT_MANAGER: RoleCode.AMR,
        RoleLevel.MANAGER: RoleCode.MGR,
        RoleLevel.SENIOR_MANAGER: RoleCode.SMR,
        # The source pairs Specialist Leader with Senior Manager in one cell.
        RoleLevel.SPECIALIST_LEADER: RoleCode.SMR,
        RoleLevel.DIRECTOR: None,
        RoleLevel.PARTNER: RoleCode.PTR,
    }
)


def code_for_level(level: RoleLevel) -> RoleCode | None:
    """The suggested platform code for a normalized level, or None where the catalogue
    genuinely has no equivalent. A suggestion for an admin, never an automatic write."""
    return LEVEL_TO_CODE[level]


# --------------------------------------------------------------------- mapping status


class MappingStatus(StrEnum):
    """How a profile's taxonomy fields came to be, so migration can be honest.

    Existing rows predate this catalogue and carry free-text `designation` only. Rather
    than invent a title for them, migration 0018 leaves the new columns NULL and marks
    the row `unmapped` — the admin console then has a real queue to work through instead
    of a database full of plausible guesses (§19).
    """

    #: No taxonomy assigned. The state every pre-existing profile starts in.
    UNMAPPED = "unmapped"
    #: Practice, title and level all resolved against this catalogue.
    MAPPED = "mapped"
    #: The organisation uses a title this catalogue does not carry. `role_title_custom`
    #: holds it; the level is still required so the person stays comparable.
    CUSTOM = "custom"
