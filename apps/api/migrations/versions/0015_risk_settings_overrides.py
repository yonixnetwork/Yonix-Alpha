"""risk settings: engine scopes store only their overrides of GLOBAL

Each engine scope whose latest risk_settings version is a full copy (written
before overrides existed) gets a new version holding only the keys that
differ from GLOBAL (or from the engine's code defaults when no GLOBAL
version exists). The effective settings are exactly the same; from now on a
GLOBAL change reaches the engine for every key it does not override.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-27 14:00:00.000000

"""
import json
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0015'
down_revision: Union[str, None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OVERRIDES = "__overrides__"
NOTE = "converted to overrides of GLOBAL (effective settings unchanged)"


def upgrade() -> None:
    from yonixalpha_core.safety.settings import default_settings_for, settings_from_dict, settings_to_dict

    conn = op.get_bind()
    latest = conn.execute(sa.text(
        "SELECT DISTINCT ON (scope) scope, version, settings FROM risk_settings ORDER BY scope, version DESC")).fetchall()
    rows = {r.scope: r for r in latest}
    glob = rows.get("GLOBAL")
    for scope, row in rows.items():
        if scope == "GLOBAL":
            continue
        settings = row.settings if isinstance(row.settings, dict) else json.loads(row.settings)
        if OVERRIDES in settings:
            continue
        gsettings = None
        if glob is not None:
            gsettings = glob.settings if isinstance(glob.settings, dict) else json.loads(glob.settings)
        base = settings_to_dict(settings_from_dict(gsettings)) if gsettings is not None \
            else settings_to_dict(default_settings_for(scope))
        full = settings_to_dict(settings_from_dict(settings))
        overrides = {k: v for k, v in full.items() if base.get(k) != v}
        conn.execute(sa.text(
            "INSERT INTO risk_settings (id, scope, version, settings, note, created_at) "
            "VALUES (:id, :scope, :version, CAST(:settings AS JSONB), :note, now())"),
            {"id": str(uuid.uuid4()), "scope": scope, "version": row.version + 1,
             "settings": json.dumps({OVERRIDES: overrides}), "note": NOTE})


def downgrade() -> None:
    # Versions are append-only history; the converted rows stay (a full copy
    # can be written again from the dashboard).
    pass
