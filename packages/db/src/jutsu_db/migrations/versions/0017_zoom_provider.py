"""Zoom joins the provider catalogue and the source_system enum.

Two data-plane facts make Zoom a migration rather than a registry edit (the rule
migration 0012 wrote down: adding a provider is a migration on purpose):

* `connections.provider` and `connection_policies.provider` are CHECK-constrained to
  the catalogue, so the constraint must learn the new id before a row can carry it.
* `sources.system` is the `source_system` enum, and a sync creates a source row in
  the provider's ACL namespace — `zoom` — so the enum must learn the value too.

`ALTER TYPE ... ADD VALUE` runs in an autocommit block: Postgres accepts it inside a
transaction since v12 but refuses to *use* the value before commit, and Alembic's
surrounding transaction would make that ordering easy to trip over later.

Downgrade restores the eleven-provider constraints as NOT VALID: existing zoom
rows are grandfathered (visibly — `convalidated` is false in pg_constraint) while
new ones are refused. Refusing the downgrade outright, the way `migrate-pg-down`
refuses over superseded documents, would be guarding the wrong thing: a zoom row
under pre-zoom code fails closed at the catalogue lookup (`PROVIDERS.get` finds
nothing and the sync refuses), nothing misreads it — and downgrade-to-base is the
test harness's reset path, which a data-dependent refusal would wedge on the first
seeded row. Deleting the rows stays an operator's decision, never a migration's
side effect. The enum keeps the `zoom` value either way; Postgres cannot drop enum
values, and an unused one is harmless.
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

#: Mirrors `jutsu_core.providers.PROVIDERS`; the parity test inserts every id.
PROVIDERS = (
    "google_drive",
    "gmail",
    "google_calendar",
    "google_meet",
    "onedrive",
    "teams",
    "sharepoint",
    "slack",
    "jira",
    "confluence",
    "zoom",
    "github",
)

_OLD_PROVIDERS = tuple(p for p in PROVIDERS if p != "zoom")

_TABLES = ("connections", "connection_policies")


def _provider_list(providers: tuple[str, ...]) -> str:
    return ", ".join(f"'{p}'" for p in providers)


def _swap_checks(providers: tuple[str, ...], *, validate: bool) -> None:
    suffix = "" if validate else " NOT VALID"
    for table in _TABLES:
        # Raw SQL on purpose: the ck_ prefix comes from the metadata naming
        # convention, and spelling the final name here keeps the migration honest
        # about what it drops rather than trusting two conventions to agree.
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT ck_{table}_provider")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_provider "
            f"CHECK (provider IN ({_provider_list(providers)})){suffix}"
        )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE source_system ADD VALUE IF NOT EXISTS 'zoom'")
    _swap_checks(PROVIDERS, validate=True)


def downgrade() -> None:
    _swap_checks(_OLD_PROVIDERS, validate=False)
