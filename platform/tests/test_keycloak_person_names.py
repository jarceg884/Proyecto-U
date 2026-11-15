"""Display names Keycloak refuses (hallazgo 0.7.x: invitar a "Ana Pérez (Ventas)" fallaba).

Keycloak's default user profile validates firstName/lastName with
`person-name-prohibited-characters`. The invite copied the display name as typed into
firstName/lastName, Keycloak answered 400 error-person-name-invalid-character and the whole
invite failed. The directory keeps the name as typed; the identity provider gets the name
without the characters it refuses.
"""
import os
import uuid

import pytest

from app.tenants.keycloak_admin import keycloak_person_names
from tests.test_auth_panel import KC_BASE, _kc_available
from tests.test_e2e_keycloak import ADMIN_PASSWORD, ADMIN_USER

REALM = "trust-agent-panel"  # shared with test_auth_panel (ensure_realm is idempotent)


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [
        ("Ana Pérez (Ventas)", ("Ana", "Pérez Ventas")),
        ("José [QA] <ops> & Co", ("José", "QA ops Co")),
        ("María", ("María", "Admin")),
        ("((()))", ("Admin", "Admin")),
        (None, ("Admin", "Admin")),
        ("  Lucía   de la  Cruz ", ("Lucía", "de la Cruz")),
    ],
)
def test_person_names_drop_what_keycloak_refuses(display_name, expected):
    assert keycloak_person_names(display_name, fallback_first="Admin", fallback_last="Admin") == expected


@pytest.fixture(scope="module")
def kc(env):
    if not _kc_available():
        pytest.skip("Keycloak local (127.0.0.1:8080) not reachable")
    from app.config import Settings
    from app.tenants.keycloak_admin import KeycloakAdminClient

    client = KeycloakAdminClient(Settings(
        database_url=os.environ["TRUST_DATABASE_URL"],
        keycloak_admin_url=KC_BASE,
        keycloak_admin_user=ADMIN_USER,
        keycloak_admin_password=ADMIN_PASSWORD,
    ))
    client.ensure_realm(REALM)
    client.enable_unmanaged_attributes(REALM)
    return client


def test_keycloak_refuses_parentheses_in_a_person_name(kc):
    """Root cause, against the real Keycloak: the name as typed is refused."""
    username = f"names-raw-{uuid.uuid4().hex[:8]}"
    resp = kc._request(
        "POST", f"/admin/realms/{REALM}/users", step="test.names_raw",
        json_body={"username": username, "enabled": True, "email": f"{username}@example.test",
                   "firstName": "Ana", "lastName": "Pérez (Ventas)"},
    )
    try:
        assert resp.status_code == 400, resp.text
        assert "person-name-invalid-character" in resp.text
    finally:
        created = kc.find_user(REALM, username)
        if created:
            kc._request("DELETE", f"/admin/realms/{REALM}/users/{created['id']}", step="test.cleanup")


def test_invite_with_parentheses_reaches_keycloak(kc):
    username = f"names-invite-{uuid.uuid4().hex[:8]}"
    user_id, one_time_password = kc.ensure_user_with_one_time_password(
        REALM, username=username, email=f"{username}@example.test",
        tenant_id=str(uuid.uuid4()), display_name="Ana Pérez (Ventas)",
    )
    try:
        assert one_time_password
        created = kc.find_user(REALM, username)
        assert created and created["id"] == user_id
        assert (created.get("firstName"), created.get("lastName")) == ("Ana", "Pérez Ventas")
    finally:
        kc._request("DELETE", f"/admin/realms/{REALM}/users/{user_id}", step="test.cleanup")
