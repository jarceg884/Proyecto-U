"""Migración 0050: audit action para un presupuesto forzado por debajo de lo ya asignado.

Spec 005 (evolución 0.7.4): bajar el presupuesto de la organización por debajo de la suma de
los departamentos, o el de un departamento por debajo de la suma de sus usuarios, deja el nivel
de abajo sobre-asignado. Como bajar por debajo del gasto, exige ``force=true`` y queda auditado
con su propia acción (``budget.forced_below_spend`` sigue siendo solo la del gasto).

Revision ID: 0050_budget_forced_below_alloc
Revises: 0049_marketplace_sections
Create Date: 20261116

Creada vía db/scripts/migration_guard.py (lock de serialización — ADR 0001); renombrada a 0050
para seguir la secuencia real (el guardián tomó el 1900 de 20261110_1900_policy_revision.py).
"""
from alembic import op

revision = "0050_budget_forced_below_alloc"
down_revision = "0049_marketplace_sections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO catalog.audit_actions (action, description) VALUES "
        "  ('budget.forced_below_allocations', "
        "   'Budget reduced below what is already allocated to the level below "
        "(departments or users) with force=true.') "
        "ON CONFLICT (action) DO NOTHING"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM catalog.audit_actions WHERE action = 'budget.forced_below_allocations'"
    )
