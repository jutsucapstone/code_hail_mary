"""The role taxonomy: seven catalogue tables, six profile columns, one permission.

Two source documents drive this (see `docs/adr/0015-role-taxonomy.md`), and the schema's
job is to keep them apart:

* the Deloitte taxonomy supplies **practice → (discipline) → title** plus a *normalized
  level* that makes seniority comparable across practices whose vocabularies do not
  match — Audit says "Audit Senior Assistant" where Tax says "Tax Consultant I";
* the JUTSU role-code document supplies **eleven platform codes** with tiers, four of
  which are governance seats.

Three properties are enforced by the schema rather than by review:

* **A title cannot escape its practice.** `role_titles` carries a redundant
  `UNIQUE (key, practice_key)` purely so `employee_profiles` can hold a *composite*
  foreign key on `(role_title_key, practice_key)`. That is what makes
  "Technology + Tax Consultant I" a database error rather than a validation rule somebody
  can forget to call (§18).
* **A level cannot escape its title.** The same trick against `role_title_levels`:
  `(role_title_key, role_level_key)` must be a pair the source document admits. Where the
  source is genuinely ambiguous — "Audit Senior → Consultant / Senior Consultant" — both
  rows exist and either is accepted; where it is not, the wrong level is refused. Both
  composite keys use MATCH SIMPLE, so a NULL in any column satisfies them and a
  half-assigned profile stays legal.
* **The catalogue is migration-owned.** Exactly like `roles`/`permissions` in 0002, the
  application role keeps SELECT and loses INSERT/UPDATE/DELETE. A compromised request
  path cannot mint a role code, so `CHM` cannot be created at runtime.

`member:assign_role_code` is a NEW permission rather than a reuse of
`member:assign_role`, because a platform code is not an RBAC role and the day the two
share a guard is the day one is mistaken for the other. It goes to owner, super_admin and
hr_admin — personnel mapping is HR's domain, which is also what the source document says
HRA controls. The four *privileged* codes need more than the permission; that rule
depends on the actor's role rank and so lives in `jutsu_api.roles`, not here.

Existing profiles are NOT back-filled. Every pre-existing row keeps its free-text
`designation` untouched and starts `unmapped`, which gives the admin console a real queue
to work through instead of a table full of plausible guesses (§19).

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

ASSIGN_ROLE_CODE = "member:assign_role_code"
ASSIGN_ROLE_CODE_ROLES = ("owner", "super_admin", "hr_admin")

#: Mirrors `jutsu_core.taxonomy`. Duplicated on purpose — a migration that imported
#: application code would rewrite history whenever that code changed — and
#: `test_taxonomy_catalogue.py` asserts the two are identical so they cannot drift.

PRACTICES = (
    ("technology", "Technology", 10),
    ("quality_assurance", "Quality Assurance / Testing", 20),
    ("audit_assurance", "Audit & Assurance", 30),
    ("tax_legal", "Tax & Legal Services", 40),
    ("risk_financial_advisory", "Risk & Financial Advisory", 50),
)

DISCIPLINES = (
    ("software_engineering", "technology", "Software Engineering", 10),
    ("data_analytics", "technology", "Data & Analytics", 20),
    ("cloud_devops", "technology", "Cloud & DevOps", 30),
)

LEVELS = (
    ("analyst", "Analyst", 10, "Entry-grade delivery and research."),
    ("bta", "BTA", 10, "Business Technology Analyst; entry-grade peer of Analyst."),
    ("senior_analyst", "Senior Analyst", 20, "Analyst-grade with ownership of a workstream."),
    ("consultant", "Consultant", 30, "Independent delivery against a defined scope."),
    (
        "consultant_senior",
        "Consultant (senior)",
        35,
        "The upper Consultant band; Tax names it explicitly as Tax Consultant II.",
    ),
    (
        "senior_consultant",
        "Senior Consultant",
        40,
        "Technical lead and senior operational delivery.",
    ),
    (
        "assistant_manager",
        "Assistant Manager",
        50,
        "Junior management; named explicitly on the Risk ladder.",
    ),
    ("manager", "Manager", 60, "Project leadership and delivery oversight."),
    ("senior_manager", "Senior Manager", 70, "Portfolio and multi-project oversight."),
    (
        "specialist_leader",
        "Specialist Leader",
        70,
        "Deep-specialist track; the source pairs it with Senior Manager.",
    ),
    ("director", "Director", 80, "Named on the extended Risk Advisory ladder, below Partner."),
    ("partner", "Partner", 90, "Practice leadership and ownership."),
)

TITLES = (
    (
        "associate_software_engineer",
        "technology",
        "software_engineering",
        "Associate Software Engineer",
        10,
    ),
    ("software_engineer", "technology", "software_engineering", "Software Engineer", 20),
    (
        "senior_software_engineer",
        "technology",
        "software_engineering",
        "Senior Software Engineer",
        30,
    ),
    (
        "engineering_lead_manager",
        "technology",
        "software_engineering",
        "Engineering Lead / Manager",
        40,
    ),
    (
        "principal_engineer_architect",
        "technology",
        "software_engineering",
        "Principal Engineer / Architect",
        50,
    ),
    ("data_analyst", "technology", "data_analytics", "Data Analyst", 60),
    ("data_engineer", "technology", "data_analytics", "Data Engineer", 70),
    (
        "senior_data_scientist_engineer",
        "technology",
        "data_analytics",
        "Senior Data Scientist / Engineer",
        80,
    ),
    ("analytics_manager", "technology", "data_analytics", "Analytics Manager", 90),
    ("data_ai_architect", "technology", "data_analytics", "Data/AI Architect", 100),
    ("cloud_associate", "technology", "cloud_devops", "Cloud Associate", 110),
    ("devops_engineer", "technology", "cloud_devops", "DevOps Engineer", 120),
    ("senior_cloud_engineer", "technology", "cloud_devops", "Senior Cloud Engineer", 130),
    ("cloud_solutions_manager", "technology", "cloud_devops", "Cloud Solutions Manager", 140),
    ("cloud_architect", "technology", "cloud_devops", "Cloud Architect", 150),
    ("qa_analyst", "quality_assurance", None, "QA Analyst", 160),
    ("qa_engineer", "quality_assurance", None, "QA Engineer", 170),
    (
        "senior_qa_engineer_automation_lead",
        "quality_assurance",
        None,
        "Senior QA Engineer / Automation Lead",
        180,
    ),
    ("qa_manager", "quality_assurance", None, "QA Manager", 190),
    ("audit_assistant_analyst", "audit_assurance", None, "Audit Assistant / Analyst", 200),
    ("audit_senior_assistant", "audit_assurance", None, "Audit Senior Assistant", 210),
    ("audit_senior", "audit_assurance", None, "Audit Senior (Audit In-Charge)", 220),
    ("audit_manager", "audit_assurance", None, "Audit Manager", 230),
    ("audit_senior_manager", "audit_assurance", None, "Audit Senior Manager", 240),
    ("partner_managing_director", "audit_assurance", None, "Partner / Managing Director", 250),
    ("tax_analyst_associate", "tax_legal", None, "Tax Analyst / Associate Analyst", 260),
    ("tax_consultant_i", "tax_legal", None, "Tax Consultant I", 270),
    ("tax_consultant_ii", "tax_legal", None, "Tax Consultant II", 280),
    ("senior_tax_consultant", "tax_legal", None, "Senior Tax Consultant", 290),
    ("tax_manager", "tax_legal", None, "Tax Manager", 300),
    ("tax_senior_manager", "tax_legal", None, "Tax Senior Manager", 310),
    ("risk_analyst", "risk_financial_advisory", None, "Risk Analyst", 320),
    ("senior_risk_analyst", "risk_financial_advisory", None, "Senior Risk Analyst", 330),
    ("risk_consultant", "risk_financial_advisory", None, "Risk Consultant", 340),
    (
        "assistant_manager_risk_advisory",
        "risk_financial_advisory",
        None,
        "Assistant Manager, Risk Advisory",
        350,
    ),
    ("cyber_security_analyst", "risk_financial_advisory", None, "Cyber Security Analyst", 360),
    ("cyber_risk_consultant", "risk_financial_advisory", None, "Cyber Risk Consultant", 370),
    ("risk_advisory_manager", "risk_financial_advisory", None, "Risk Advisory Manager", 380),
    (
        "financial_advisory_analyst",
        "risk_financial_advisory",
        None,
        "Financial Advisory Analyst (M&A/Valuations)",
        390,
    ),
    (
        "financial_advisory_senior_consultant",
        "risk_financial_advisory",
        None,
        "Financial Advisory Senior Consultant",
        400,
    ),
    ("risk_advisory_director", "risk_financial_advisory", None, "Risk Advisory Director", 410),
)

#: `is_default` is True on the source document's FIRST-listed level. It is the documented
#: tie-break for the one place a single value is unavoidable (a suggestion in the admin
#: UI); it never narrows what may be assigned.
TITLE_LEVELS = (
    ("associate_software_engineer", "analyst", True),
    ("associate_software_engineer", "bta", False),
    ("software_engineer", "consultant", True),
    ("senior_software_engineer", "senior_consultant", True),
    ("engineering_lead_manager", "manager", True),
    ("principal_engineer_architect", "senior_manager", True),
    ("data_analyst", "analyst", True),
    ("data_engineer", "consultant", True),
    ("senior_data_scientist_engineer", "senior_consultant", True),
    ("analytics_manager", "manager", True),
    ("data_ai_architect", "senior_manager", True),
    ("data_ai_architect", "specialist_leader", False),
    ("cloud_associate", "analyst", True),
    ("devops_engineer", "consultant", True),
    ("senior_cloud_engineer", "senior_consultant", True),
    ("cloud_solutions_manager", "manager", True),
    ("cloud_architect", "senior_manager", True),
    ("cloud_architect", "specialist_leader", False),
    ("qa_analyst", "analyst", True),
    ("qa_engineer", "consultant", True),
    ("senior_qa_engineer_automation_lead", "senior_consultant", True),
    ("qa_manager", "manager", True),
    ("audit_assistant_analyst", "analyst", True),
    ("audit_senior_assistant", "senior_analyst", True),
    ("audit_senior", "consultant", True),
    ("audit_senior", "senior_consultant", False),
    ("audit_manager", "manager", True),
    ("audit_senior_manager", "senior_manager", True),
    ("partner_managing_director", "partner", True),
    ("tax_analyst_associate", "analyst", True),
    ("tax_consultant_i", "consultant", True),
    ("tax_consultant_ii", "consultant_senior", True),
    ("senior_tax_consultant", "senior_consultant", True),
    ("tax_manager", "manager", True),
    ("tax_senior_manager", "senior_manager", True),
    ("risk_analyst", "analyst", True),
    ("senior_risk_analyst", "senior_analyst", True),
    ("risk_consultant", "consultant", True),
    ("assistant_manager_risk_advisory", "assistant_manager", True),
    ("cyber_security_analyst", "analyst", True),
    ("cyber_security_analyst", "consultant", False),
    ("cyber_risk_consultant", "consultant", True),
    ("cyber_risk_consultant", "senior_consultant", False),
    ("risk_advisory_manager", "manager", True),
    ("financial_advisory_analyst", "analyst", True),
    ("financial_advisory_senior_consultant", "senior_consultant", True),
    ("risk_advisory_director", "director", True),
)

CODES = (
    (
        "CHM",
        "Chairman / Superadmin",
        8,
        "executive",
        "The ultimate global controller. Possesses absolute root access to everything.",
        True,
    ),
    (
        "CEO",
        "Chief Executive Officer / Admin",
        7,
        "executive",
        "The primary operational controller acting directly underneath the Chairman.",
        True,
    ),
    (
        "ITA",
        "IT Admin Controller",
        6,
        "executive",
        "Controls all security, tooling and application provisioning. Direct report to CEO.",
        True,
    ),
    (
        "HRA",
        "HR Admin Controller",
        6,
        "executive",
        "Controls personnel mapping, knowledge-transfer matrices and role data. Direct report to CEO.",
        True,
    ),
    ("PTR", "Partner", 6, "execution", "Practice leadership and portfolio execution.", False),
    ("SMR", "Senior Manager", 5, "execution", "Portfolio and multi-project oversight.", False),
    (
        "MGR",
        "Manager",
        4,
        "execution",
        "Primary project leadership and client delivery oversight.",
        False,
    ),
    (
        "AMR",
        "Assistant Manager",
        3,
        "execution",
        "Specialised operations and junior management.",
        False,
    ),
    (
        "SCN",
        "Senior Consultant",
        2,
        "execution",
        "Senior operational delivery and technical lead.",
        False,
    ),
    ("CON", "Consultant", 1, "execution", "Core execution and independent delivery.", False),
    (
        "ANS",
        "Analyst",
        1,
        "execution",
        "Associate delivery, research and technical support.",
        False,
    ),
)

#: The mapping POLICY. `director` maps to NULL on purpose: the JUTSU catalogue jumps SMR
#: (T5) to PTR (T6) with nothing between, and Director sits between Senior Manager and
#: Partner on the Risk ladder, so either choice would invent a promotion or a demotion.
#: No level maps to CHM/CEO/ITA/HRA — a governance seat is never implied by seniority.
LEVEL_CODES = (
    ("analyst", "ANS"),
    ("bta", "ANS"),
    ("senior_analyst", "ANS"),
    ("consultant", "CON"),
    ("consultant_senior", "CON"),
    ("senior_consultant", "SCN"),
    ("assistant_manager", "AMR"),
    ("manager", "MGR"),
    ("senior_manager", "SMR"),
    ("specialist_leader", "SMR"),
    ("director", None),
    ("partner", "PTR"),
)

#: Every catalogue table here is GLOBAL reference data: no `org_id`, no RLS, one row set
#: for the whole deployment — the same shape as `roles`/`permissions`. Only
#: `employee_profiles` (RLS-forced since 0002) carries the per-tenant assignment.
CATALOGUE_TABLES = (
    "role_practices",
    "role_disciplines",
    "role_levels",
    "role_titles",
    "role_title_levels",
    "role_codes",
    "role_level_codes",
)


def upgrade() -> None:
    # ------------------------------------------------------------------- permission
    op.bulk_insert(
        sa.table("permissions", sa.column("key", sa.String), sa.column("description", sa.Text)),
        [
            {
                "key": ASSIGN_ROLE_CODE,
                "description": (
                    "Assign an employee's role taxonomy and platform role code. "
                    "Grants no document and confers no RBAC permission."
                ),
            }
        ],
    )
    op.bulk_insert(
        sa.table(
            "role_permissions",
            sa.column("role_key", sa.String),
            sa.column("permission_key", sa.String),
        ),
        [{"role_key": role, "permission_key": ASSIGN_ROLE_CODE} for role in ASSIGN_ROLE_CODE_ROLES],
    )

    # -------------------------------------------------------------------- practices
    op.create_table(
        "role_practices",
        sa.Column("key", sa.String(48), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("sort_order", sa.SmallInteger, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.bulk_insert(
        sa.table(
            "role_practices",
            sa.column("key", sa.String),
            sa.column("display_name", sa.String),
            sa.column("sort_order", sa.SmallInteger),
        ),
        [{"key": k, "display_name": d, "sort_order": s} for k, d, s in PRACTICES],
    )

    # ------------------------------------------------------------------ disciplines
    op.create_table(
        "role_disciplines",
        sa.Column("key", sa.String(48), primary_key=True),
        sa.Column(
            "practice_key",
            sa.String(48),
            sa.ForeignKey("role_practices.key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("sort_order", sa.SmallInteger, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.bulk_insert(
        sa.table(
            "role_disciplines",
            sa.column("key", sa.String),
            sa.column("practice_key", sa.String),
            sa.column("display_name", sa.String),
            sa.column("sort_order", sa.SmallInteger),
        ),
        [
            {"key": k, "practice_key": p, "display_name": d, "sort_order": s}
            for k, p, d, s in DISCIPLINES
        ],
    )

    # ----------------------------------------------------------------------- levels
    op.create_table(
        "role_levels",
        sa.Column("key", sa.String(48), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        # Deliberately NOT unique: "Senior Manager / Specialist Leader" and
        # "Analyst / BTA" are peers in the source, and a unique rank would invent an
        # ordering the document does not claim.
        sa.Column("rank", sa.SmallInteger, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.bulk_insert(
        sa.table(
            "role_levels",
            sa.column("key", sa.String),
            sa.column("display_name", sa.String),
            sa.column("rank", sa.SmallInteger),
            sa.column("description", sa.Text),
        ),
        [{"key": k, "display_name": d, "rank": r, "description": x} for k, d, r, x in LEVELS],
    )

    # ----------------------------------------------------------------------- titles
    op.create_table(
        "role_titles",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column(
            "practice_key",
            sa.String(48),
            sa.ForeignKey("role_practices.key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "discipline_key",
            sa.String(48),
            sa.ForeignKey("role_disciplines.key", ondelete="RESTRICT"),
        ),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("sort_order", sa.SmallInteger, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        # Redundant against the primary key, and load-bearing: it is the target of the
        # composite foreign key that stops a profile pairing a title with the wrong
        # practice.
        sa.UniqueConstraint("key", "practice_key", name="uq_role_titles_key_practice_key"),
    )
    op.bulk_insert(
        sa.table(
            "role_titles",
            sa.column("key", sa.String),
            sa.column("practice_key", sa.String),
            sa.column("discipline_key", sa.String),
            sa.column("display_name", sa.String),
            sa.column("sort_order", sa.SmallInteger),
        ),
        [
            {
                "key": k,
                "practice_key": p,
                "discipline_key": d,
                "display_name": n,
                "sort_order": s,
            }
            for k, p, d, n, s in TITLES
        ],
    )

    # ---------------------------------------------------------------- title → level
    op.create_table(
        "role_title_levels",
        sa.Column(
            "title_key",
            sa.String(64),
            sa.ForeignKey("role_titles.key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "level_key",
            sa.String(48),
            sa.ForeignKey("role_levels.key", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("is_default", sa.Boolean, nullable=False),
    )
    op.bulk_insert(
        sa.table(
            "role_title_levels",
            sa.column("title_key", sa.String),
            sa.column("level_key", sa.String),
            sa.column("is_default", sa.Boolean),
        ),
        [{"title_key": t, "level_key": lv, "is_default": d} for t, lv, d in TITLE_LEVELS],
    )

    # ------------------------------------------------------------------ role codes
    op.create_table(
        "role_codes",
        sa.Column("code", sa.String(3), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("tier", sa.SmallInteger, nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        # The four governance seats. Assignment needs more than the permission — see
        # `jutsu_api.roles` — but the flag lives here so the API and the UI agree on
        # which codes are dangerous without either hardcoding a list.
        sa.Column("privileged", sa.Boolean, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.CheckConstraint("category IN ('executive', 'execution')", name="category"),
        sa.CheckConstraint("tier BETWEEN 1 AND 8", name="tier"),
    )
    op.bulk_insert(
        sa.table(
            "role_codes",
            sa.column("code", sa.String),
            sa.column("display_name", sa.String),
            sa.column("tier", sa.SmallInteger),
            sa.column("category", sa.String),
            sa.column("description", sa.Text),
            sa.column("privileged", sa.Boolean),
        ),
        [
            {
                "code": c,
                "display_name": d,
                "tier": t,
                "category": cat,
                "description": desc,
                "privileged": priv,
            }
            for c, d, t, cat, desc, priv in CODES
        ],
    )

    # -------------------------------------------------------------- level → code map
    op.create_table(
        "role_level_codes",
        sa.Column(
            "level_key",
            sa.String(48),
            sa.ForeignKey("role_levels.key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("code", sa.String(3), sa.ForeignKey("role_codes.code", ondelete="RESTRICT")),
    )
    op.bulk_insert(
        sa.table(
            "role_level_codes",
            sa.column("level_key", sa.String),
            sa.column("code", sa.String),
        ),
        [{"level_key": lv, "code": c} for lv, c in LEVEL_CODES],
    )

    # -------------------------------------------------------------- profile columns
    op.add_column("employee_profiles", sa.Column("practice_key", sa.String(48)))
    op.add_column("employee_profiles", sa.Column("role_title_key", sa.String(64)))
    op.add_column("employee_profiles", sa.Column("role_level_key", sa.String(48)))
    op.add_column("employee_profiles", sa.Column("role_code", sa.String(3)))
    op.add_column("employee_profiles", sa.Column("role_title_custom", sa.String(128)))
    op.add_column(
        "employee_profiles",
        sa.Column("role_mapping_status", sa.String(16), nullable=False, server_default="unmapped"),
    )

    op.create_foreign_key(
        "fk_employee_profiles_practice_key",
        "employee_profiles",
        "role_practices",
        ["practice_key"],
        ["key"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_employee_profiles_role_level_key",
        "employee_profiles",
        "role_levels",
        ["role_level_key"],
        ["key"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_employee_profiles_role_code",
        "employee_profiles",
        "role_codes",
        ["role_code"],
        ["code"],
        ondelete="RESTRICT",
    )
    # The two composite keys that make an impossible combination unrepresentable.
    op.create_foreign_key(
        "fk_employee_profiles_title_practice",
        "employee_profiles",
        "role_titles",
        ["role_title_key", "practice_key"],
        ["key", "practice_key"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_employee_profiles_title_level",
        "employee_profiles",
        "role_title_levels",
        ["role_title_key", "role_level_key"],
        ["title_key", "level_key"],
        ondelete="RESTRICT",
    )

    op.create_check_constraint(
        "role_mapping_status",
        "employee_profiles",
        "role_mapping_status IN ('unmapped', 'mapped', 'custom')",
    )
    # The status is a claim about the other columns, so a row states it truthfully or it
    # does not exist. `custom` must carry the free-text title AND a level, because a
    # custom title with no level is invisible to every cross-practice query — which is
    # the one thing the taxonomy exists to make possible.
    op.create_check_constraint(
        "role_mapping_coherent",
        "employee_profiles",
        """
        (role_mapping_status = 'unmapped'
            AND role_title_key IS NULL AND role_title_custom IS NULL)
        OR (role_mapping_status = 'mapped'
            AND role_title_key IS NOT NULL AND role_level_key IS NOT NULL
            AND practice_key IS NOT NULL AND role_title_custom IS NULL)
        OR (role_mapping_status = 'custom'
            AND role_title_custom IS NOT NULL AND role_level_key IS NOT NULL
            AND role_title_key IS NULL)
        """,
    )

    # Two indexes, both for filters the admin console and Expert Finder actually issue:
    # "everyone at this seniority" and "everyone in this practice". `role_code` is left
    # unindexed — it is lower-cardinality than either and the table is per-tenant small,
    # so an index would cost writes to save nothing measurable.
    op.create_index(
        "ix_employee_profiles_org_id_role_level_key",
        "employee_profiles",
        ["org_id", "role_level_key"],
    )
    op.create_index(
        "ix_employee_profiles_org_id_practice_key",
        "employee_profiles",
        ["org_id", "practice_key"],
    )

    # -------------------------------------------------------- catalogue is read-only
    # Written out longhand rather than interpolated from CATALOGUE_TABLES: the tuple is
    # a module constant and could not carry caller input, but a REVOKE assembled by
    # f-string is exactly the shape a reader has to stop and verify, and the linter is
    # right to make that cost visible.
    op.execute(
        """
        DO $do$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jutsu_app') THEN
            REVOKE INSERT, UPDATE, DELETE ON
              role_practices, role_disciplines, role_levels, role_titles,
              role_title_levels, role_codes, role_level_codes
            FROM jutsu_app;
          END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_employee_profiles_org_id_practice_key", table_name="employee_profiles")
    op.drop_index("ix_employee_profiles_org_id_role_level_key", table_name="employee_profiles")
    op.drop_constraint("ck_employee_profiles_role_mapping_coherent", "employee_profiles")
    op.drop_constraint("ck_employee_profiles_role_mapping_status", "employee_profiles")
    for name in (
        "fk_employee_profiles_title_level",
        "fk_employee_profiles_title_practice",
        "fk_employee_profiles_role_code",
        "fk_employee_profiles_role_level_key",
        "fk_employee_profiles_practice_key",
    ):
        op.drop_constraint(name, "employee_profiles", type_="foreignkey")
    for column in (
        "role_mapping_status",
        "role_title_custom",
        "role_code",
        "role_level_key",
        "role_title_key",
        "practice_key",
    ):
        op.drop_column("employee_profiles", column)

    for table in reversed(CATALOGUE_TABLES):
        op.drop_table(table)

    # Explicit rather than relying on the FK's cascade, matching 0005 and 0009 — the
    # reversal reads the same way round as the application and does not depend on
    # `ondelete` staying as it is.
    op.execute(
        sa.text("DELETE FROM role_permissions WHERE permission_key = :p").bindparams(
            p=ASSIGN_ROLE_CODE
        )
    )
    op.execute(sa.text("DELETE FROM permissions WHERE key = :p").bindparams(p=ASSIGN_ROLE_CODE))
