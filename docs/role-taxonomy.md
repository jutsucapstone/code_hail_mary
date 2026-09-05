# The role taxonomy

Two source documents, deliberately kept as two things:

- **`Deloitte_USI_Role_Taxonomy.pdf`** — the business dimensions: practice, role title, and
  a normalized seniority level.
- **`mindmap of role codes.pdf`** — the platform dimension: eleven JUTSU role codes.

Neither PDF lives in the repository; both are the authority for the *content* of the
catalogue, and `docs/adr/0015-role-taxonomy.md` argues the architecture. This page is the
operator's and developer's reference.

## Why three fields and not one

A title cannot be compared across practices, because each practice runs its own
vocabulary for the same rung:

| Practice | Their word for a Senior-Consultant-grade person |
| --- | --- |
| Technology | Senior Software Engineer |
| Audit & Assurance | Audit Senior (Audit In-Charge) |
| Tax & Legal | Senior Tax Consultant |
| Risk & Financial Advisory | Financial Advisory Senior Consultant |

Matching title strings finds none of the equivalences. So every title also carries a
**normalized level**, and it is the level that answers "find the Senior-Consultant-grade
people who worked on Project X". The title is still stored and still displayed — comparing
people must not rename them.

The **platform role code** is a third thing again: where somebody sits on the org chart.

## The data model

Seven global catalogue tables, seeded by migration 0018 and read-only to the application:

| Table | What it holds |
| --- | --- |
| `role_practices` | 5 business lines |
| `role_disciplines` | 3 sub-functions, all inside Technology |
| `role_levels` | 12 normalized levels, each with a non-unique `rank` |
| `role_titles` | 41 titles, each belonging to exactly one practice |
| `role_title_levels` | 47 rows — the title→level matrix, including ambiguity |
| `role_codes` | the 11 JUTSU codes, with `tier` and `privileged` |
| `role_level_codes` | the level→code mapping policy |

Per-tenant assignment lives on `employee_profiles`, which has been RLS-forced since
migration 0002:

`practice_key`, `role_title_key`, `role_level_key`, `role_code`, `role_title_custom`,
`role_mapping_status`.

`designation` is untouched and keeps its old meaning: free text the employee writes about
themselves.

### What the schema refuses

Two composite foreign keys make impossible combinations unrepresentable rather than
merely validated:

- `(role_title_key, practice_key)` → `role_titles(key, practice_key)`, so
  "Technology + Tax Consultant I" is a foreign-key violation.
- `(role_title_key, role_level_key)` → `role_title_levels(title_key, level_key)`, so a
  level outside a title's admitted set is refused.

Both use MATCH SIMPLE, so a NULL in either column satisfies them and a partly-assigned
profile stays legal. A CHECK constraint keeps `role_mapping_status` honest about the
columns beside it.

## The role codes

| Code | Name | Tier | Category | Governance seat |
| --- | --- | --- | --- | --- |
| CHM | Chairman / Superadmin | T8 | Executive | yes |
| CEO | Chief Executive Officer / Admin | T7 | Executive | yes |
| ITA | IT Admin Controller | T6 | Executive | yes |
| HRA | HR Admin Controller | T6 | Executive | yes |
| PTR | Partner | T6 | Execution | no |
| SMR | Senior Manager | T5 | Execution | no |
| MGR | Manager | T4 | Execution | no |
| AMR | Assistant Manager | T3 | Execution | no |
| SCN | Senior Consultant | T2 | Execution | no |
| CON | Consultant | T1 | Execution | no |
| ANS | Analyst | T1 | Execution | no |

**A role code grants nothing.** `rbac.py` decides what a caller may do; the taxonomy never
does. `HRA` resembles `Role.HR_ADMIN` and `ITA` resembles `Role.IT_ADMIN`, and that
resemblance is the trap the design is shaped around — a test asserts the two vocabularies
share no value even case-insensitively, and another seats a bare Member in `HRA` and then
checks they still cannot read the employee list.

The JUTSU ID format in the source document (`JUTSU-CHM-A1B2C3`) is illustrative. Existing
ID generation is untouched and remains authoritative; a role code is a profile field, not
part of anybody's identifier.

## Mapping policy: level → code

| Level | Code | Note |
| --- | --- | --- |
| Analyst, BTA, Senior Analyst | ANS | Senior Analyst is still analyst-grade on the Risk ladder |
| Consultant, Consultant (senior) | CON | |
| Senior Consultant | SCN | |
| Assistant Manager | AMR | |
| Manager | MGR | |
| Senior Manager, Specialist Leader | SMR | the source pairs these in one cell |
| **Director** | **(none)** | see below |
| Partner | PTR | |

**Director maps to nothing on purpose.** The JUTSU catalogue jumps SMR (T5) to PTR (T6)
with nothing between, while the extended Risk Advisory ladder places Director above Senior
Manager and below Partner. Either choice would invent a promotion or a demotion, so the
mapping is NULL and an administrator assigns the code explicitly.

**No level maps to CHM, CEO, ITA or HRA.** A governance seat is never implied by business
seniority.

The mapping is a *suggestion* surfaced to the admin UI. Nothing applies it automatically.

## Ambiguity

Six titles map to two levels because the source document does:

| Title | Levels |
| --- | --- |
| Associate Software Engineer | Analyst / BTA |
| Data/AI Architect | Senior Manager / Specialist Leader |
| Cloud Architect | Senior Manager / Specialist Leader |
| Audit Senior (Audit In-Charge) | Consultant / Senior Consultant |
| Cyber Security Analyst | Analyst / Consultant |
| Cyber Risk Consultant | Consultant / Senior Consultant |

Both rows exist and either is accepted. `is_default` marks the source's own first-listed
level and seeds a suggestion in the UI; it never narrows what may be assigned. The admin
console tells the reader when a title is ambiguous rather than silently choosing.

## Authorization

| Action | Requires |
| --- | --- |
| Read the catalogue | `profile:self_read` — every role |
| Read somebody's assignment | `member:read` |
| Assign practice / title / level / ordinary code | `member:assign_role_code` — owner, super_admin, hr_admin |
| Assign CHM, CEO, ITA or HRA | the above **and** `Role.OWNER` or `Role.SUPER_ADMIN`, and never to oneself |

`member:assign_role_code` is a separate permission from `member:assign_role` because a
platform code is not an RBAC role. Every change writes an audit row
(`member.role_taxonomy_changed`) carrying the before and after of each field that moved.

Employees see their own assignment read-only on `/me/profile`. It is not part of
`PATCH /v1/me/profile`, whose model forbids unknown fields — so self-promotion has no
route in.

## API

| Method | Path | Guard |
| --- | --- | --- |
| GET | `/v1/role-catalogue` | `profile:self_read` |
| GET | `/v1/employees/{id}/role-assignment` | `member:read` |
| PATCH | `/v1/employees/{id}/role-assignment` | `member:assign_role_code` |

`GET /v1/employees` gains `practice`, `level`, `role_code` and `unmapped` filters and
returns each person's title, level and code alongside their RBAC role. `level` is the
Expert Finder query.

`role_mapping_status` is never accepted from a client — it is derived from what was
actually assigned, and the CHECK constraint is the backstop.

## Existing data

Migration 0018 back-fills nothing. Every pre-existing profile keeps its `designation` and
starts `unmapped`. Nothing is inferred from the free text: a guess like "Sr. SWE" →
"Senior Software Engineer" would produce a database of plausible values nobody verified,
and the plausibility is what makes it dangerous.

Administrators work through the queue with the **Needs mapping** filter on
`/admin/employees`. An organisation whose vocabulary the catalogue does not carry uses the
explicit custom path: `role_title_custom` holds the free text and a normalized level is
still required, because a custom title with no level is invisible to every cross-practice
query.

## Adding to the catalogue

Adding a practice, discipline, title, level or code is a migration — the same ceremony as
adding a permission, and for the same reason: the catalogue is the runtime authority and
the application cannot write it. Edit `jutsu_core.taxonomy`, mirror the rows in a new
migration, and `test_taxonomy_catalogue.py` will fail until the two agree.
