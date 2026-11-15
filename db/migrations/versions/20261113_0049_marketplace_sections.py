"""Marketplace de perfiles: secciones y marketplace vacío por defecto (spec 045 §7.3, 0.7.2).

- ``agent_profiles.section``: la sección del marketplace en la que el admin agrupa el perfil
  (Ventas, Soporte, TI…). Opcional, texto libre de 1 a 60 caracteres.
- El marketplace de un tenant nace VACÍO: los perfiles de fábrica (Analyst, Coder, Ops) pasan a
  ser plantillas que el admin añade a mano. Los que la 0.7.0/0.7.1 sembraron solos y ninguna
  política asigna se archivan — reversible (Restaurar, o añadir la plantilla otra vez), nada se
  borra — con su evento de auditoría. Los que alguna política asigna se quedan como están.

El rol migrador tiene BYPASSRLS: la actualización alcanza a todos los tenants.
"""
from alembic import op

revision = "0049_marketplace_sections"
down_revision = "0048_agent_profiles"
branch_labels = None
depends_on = None

_ARCHIVE_UNUSED_FACTORY = """
WITH unused AS (
  SELECT p.id, p.tenant_id, p.factory_key
  FROM agent_profiles p
  WHERE p.source = 'factory'
    AND p.status = 'active'
    AND NOT EXISTS (
      SELECT 1 FROM policy_bundles b
      WHERE b.tenant_id = p.tenant_id
        AND (
          b.default_profile_id = p.id
          OR COALESCE(b.profiles, '[]'::jsonb) @> jsonb_build_array(jsonb_build_object('profile_id', p.id::text))
        )
    )
),
archived AS (
  UPDATE agent_profiles a
  SET status = 'archived'
  FROM unused u
  WHERE a.id = u.id
  RETURNING a.id, a.tenant_id, a.factory_key
)
INSERT INTO audit_log (tenant_id, actor_role, action, target_type, target_id, payload)
SELECT tenant_id, 'system', 'agent_profile.archived', 'agent_profile', id::text,
       jsonb_build_object('factory_key', factory_key, 'reason', 'marketplace_empty_by_default')
FROM archived
"""


def upgrade() -> None:
    op.execute(
        "INSERT INTO catalog.audit_actions (action, description) VALUES"
        " ('agent_profile.template_added', 'Factory template added to the tenant marketplace by an admin.')"
        " ON CONFLICT (action) DO NOTHING"
    )
    op.execute("ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS section text")
    op.execute("ALTER TABLE agent_profiles DROP CONSTRAINT IF EXISTS agent_profiles_section_len")
    op.execute(
        "ALTER TABLE agent_profiles ADD CONSTRAINT agent_profiles_section_len"
        " CHECK (section IS NULL OR char_length(section) BETWEEN 1 AND 60)"
    )
    op.execute(_ARCHIVE_UNUSED_FACTORY)


def downgrade() -> None:
    # The archived factory profiles stay archived: restoring them is an admin decision. The audit
    # action stays registered: past events keep referencing it.
    op.execute("ALTER TABLE agent_profiles DROP CONSTRAINT IF EXISTS agent_profiles_section_len")
    op.execute("ALTER TABLE agent_profiles DROP COLUMN IF EXISTS section")
