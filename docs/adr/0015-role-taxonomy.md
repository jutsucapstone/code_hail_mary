# 0015 — Role title, normalized level and platform code are three fields, not one

Status: accepted

Source documents (authoritative for the business content, not for the architecture):

- `Deloitte_USI_Role_Taxonomy.pdf` — practice-by-practice role/level tables
- `mindmap of role codes.pdf` — the eleven JUTSU platform role codes and their tiers

## Context

`employee_profiles` has carried a free-text `designation` since migration 0002, and
migration 0014 added `invitations.role_title` with the note that "a title is vocabulary;
a role is authority; only one of them is free text". That was enough while a title was
only ever displayed. It stops being enough the moment anything has to *reason* about
seniority.

The reason is in the source document itself. Each practice runs its own vocabulary for
the same rung of the ladder: Audit says "Audit Senior Assistant", Tax says "Tax
Consultant I", Risk says "Assistant Manager", Technology says "Consultant". A query for
"the Senior-Consultant-equivalent people who worked on Project X" cannot be answered by
matching title strings, because the strings do not match — that is the whole point of the
document. Equally, storing only a normalized level would lose what the person actually
does, which is the other half of every useful answer.

A second, unrelated vocabulary arrives with the role-code document: eleven codes with
tiers, four of which (`CHM`, `CEO`, `ITA`, `HRA`) are governance seats rather than
delivery grades. Two of those four look almost exactly like existing RBAC roles —
`HRA`/`Role.HR_ADMIN`, `ITA`/`Role.IT_ADMIN` — and that resemblance is the most dangerous
thing in this feature.

## Decision

**Three separate fields, one of which is a suggestion and none of which is a permission.**

1. **Practice + title** — what the person does, from a closed catalogue seeded by
   migration 0018 (`role_practices`, `role_disciplines`, `role_titles`).
2. **Normalized level** — how senior they are, on a single cross-practice ladder
   (`role_levels`, with a non-unique `rank`). This is what Expert Finder compares.
3. **Platform role code** — organisational standing (`role_codes`), assigned explicitly.

`designation` keeps its existing meaning and its existing free-text write path. It is not
redefined, not back-filled and not deprecated; it remains what an employee may type about
themselves, while the three new fields are admin-assigned.

### A role code confers nothing

`rbac.py` decides what a caller may do; nothing in the taxonomy does, and
`test_taxonomy_catalogue.py` asserts the two vocabularies share no value even
case-insensitively. `HRA` is a seat on an org chart. `Role.HR_ADMIN` is a permission set.
If one ever implied the other, a piece of HR data would have widened the control plane —
which is the same failure ADR 0010 guards against on the data plane, in the other
direction.

Assigning any code requires the new `member:assign_role_code` permission, deliberately
*not* a reuse of `member:assign_role`: the day the two share a guard is the day one is
mistaken for the other. Assigning one of the four privileged codes additionally requires
the actor to hold `Role.OWNER` or `Role.SUPER_ADMIN`, checked server-side against the
`privileged` flag the catalogue itself carries, so neither the API nor the UI hardcodes
the list.

### Ambiguity is stored, never resolved

Where the source maps one title to two levels — "Audit Senior → Consultant / Senior
Consultant", "Cyber Security Analyst → Analyst / Consultant" — both rows exist in
`role_title_levels` and an assignment must name which applies. Six titles are genuinely
ambiguous in this way. `is_default` marks the source's own first-listed level and exists
only to seed a suggestion in the admin UI; it never narrows what may be assigned.

Likewise **Director maps to no platform code**. The JUTSU catalogue jumps `SMR` (T5) to
`PTR` (T6) with nothing between, while the extended Risk Advisory ladder places Director
above Senior Manager and below Partner. Mapping it either way would invent a promotion or
a demotion, so `role_level_codes.code` is NULL for that level and an admin assigns the
code explicitly. The gap is recorded, not smoothed over.

### Impossible combinations are unrepresentable, not merely rejected

`role_titles` carries a redundant `UNIQUE (key, practice_key)` so `employee_profiles` can
hold a *composite* foreign key on `(role_title_key, practice_key)`. "Technology + Tax
Consultant I" is therefore a foreign-key violation rather than a validation rule somebody
can forget to call. The same trick against `role_title_levels` makes a level outside a
title's admitted set impossible. Both use MATCH SIMPLE, so a NULL in either column
satisfies the constraint and a partially-assigned profile stays legal.

### Existing rows are left alone

Every profile that predates this migration keeps its `designation` and starts
`role_mapping_status = 'unmapped'`. Nothing is inferred from the free text. A back-fill
that guessed "Senior Software Engineer" from the string "Sr. SWE" would produce a
database of plausible values nobody verified, and the plausibility is exactly what makes
it dangerous — an admin reviewing a queue of `unmapped` people can see what remains to be
decided, which a silently-populated column hides.

Organisations whose vocabulary the catalogue does not carry use the explicit `custom`
path: `role_title_custom` holds the free text and a normalized level is still required,
because a custom title with no level is invisible to every cross-practice query.

## Consequences

- Cross-practice seniority queries work, which is what the source document asks for.
- The catalogue is migration-owned and read-only to the application, so a compromised
  request path cannot mint `CHM` or widen which levels a title admits.
- Adding a practice, title or code is a migration — the same ceremony as adding a
  permission, and for the same reason.
- The duplication between `jutsu_core.taxonomy` and migration 0018 is real, and is the
  same trade `rbac.py` already makes. `test_taxonomy_catalogue.py` is what stops it
  drifting.
- Profiles now have two write paths with different authority: the employee's own
  business fields, and the admin-assigned taxonomy. That split is enforced in
  `jutsu_api.roles` rather than by convention.
