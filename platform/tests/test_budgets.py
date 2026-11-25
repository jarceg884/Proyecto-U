"""REQ-GOV-001…004, 012 — presupuestos jerárquicos (ACs de spec 005 §3.1)."""
from decimal import Decimal

import psycopg

from conftest import (
    DEPT_A1, DEPT_A2, DEPT_B1, TENANT_A, TENANT_B, USER_ALICE, USER_BOB,
    USER_CAROL, USER_DAVE, _admin, bearer_headers, ingest_spend, mint,
)

import os  # noqa: E402 (honor TRUST_TEST_DB como tests/conftest.py)
TEST_DB = os.environ.get("TRUST_TEST_DB", "trust_agent_platform_test")


def _put(client, path, body, tokens):
    return client.put(path, json=body, headers=bearer_headers(tokens))


def test_dept_sum_cannot_exceed_org(client, idp, admin_conn):
    """AC-GOV-001: Σ deptos ≤ org; exceso → 422 con detalle; sin org → 422."""
    alice = mint(client, idp, "alice")
    # Sin presupuesto org (tenant B no tiene dept budgets; probamos el caso
    # de exceso con el seed: A1=400000 + A2=300000 = 700000 de 1000000).
    ok = _put(client, f"/api/budgets/dept/{DEPT_A2}",
              {"amount_cents": 300000}, alice)
    assert ok.status_code == 200, ok.text
    # Subir A2 a 400001 → Σ = 800001 ≤ 1000000 → OK.
    ok = _put(client, f"/api/budgets/dept/{DEPT_A2}",
              {"amount_cents": 400001}, alice)
    assert ok.status_code == 200, ok.text
    # Subir A1 a 600000 → Σ = 1000001 > 1000000 → 422 con exceso de 1.
    bad = _put(client, f"/api/budgets/dept/{DEPT_A1}",
               {"amount_cents": 600000}, alice)
    assert bad.status_code == 422
    assert "1" in bad.json()["error"]["message"]  # detalle del exceso
    # Restaurar A2 para los tests siguientes.
    restore = _put(client, f"/api/budgets/dept/{DEPT_A2}",
                   {"amount_cents": 300000}, alice)
    assert restore.status_code == 200


def test_dept_budget_requires_org_budget(client, idp, admin_conn):
    """AC-GOV-001 (segunda parte): sin presupuesto org → 422."""
    with _admin(TEST_DB) as c:
        c.execute(
            "UPDATE budgets SET status = 'archived' "
            f"WHERE tenant_id = '{TENANT_A}' AND level = 'org'"
        )
    alice = mint(client, idp, "alice")
    resp = _put(client, f"/api/budgets/dept/{DEPT_A1}",
                {"amount_cents": 1000}, alice)
    assert resp.status_code == 422
    assert "organization has no budget" in resp.json()["error"]["message"]
    with _admin(TEST_DB) as c:
        c.execute(
            "UPDATE budgets SET status = 'active' "
            f"WHERE tenant_id = '{TENANT_A}' AND level = 'org'"
        )


def test_user_sum_cannot_exceed_dept_and_scope(client, idp, admin_conn):
    """AC-GOV-002: Σ usuarios del depto ≤ depto; usuario de otro depto → 422/403."""
    alice = mint(client, idp, "alice")
    bob = mint(client, idp, "bob")
    # Bob (dept_admin de A2) se asigna presupuesto propio: A2=300000,
    # Σ usuarios en A2 = 0 → 100000 OK.
    ok = _put(client, f"/api/budgets/user/{USER_BOB}",
              {"amount_cents": 100000}, bob)
    assert ok.status_code == 200, ok.text
    # Exceder el depto: 400000 > 300000 → 422 con detalle.
    bad = _put(client, f"/api/budgets/user/{USER_BOB}",
               {"amount_cents": 400000}, bob)
    assert bad.status_code == 422
    # Usuario de otro departamento (Carol está en A1): fuera del scope de Bob.
    cross = _put(client, f"/api/budgets/user/{USER_CAROL}",
                 {"amount_cents": 1000}, bob)
    assert cross.status_code in (403, 404)
    # Cross-tenant: DAVE es del tenant B → 404 (RLS: no existe).
    forged = _put(client, f"/api/budgets/user/{USER_DAVE}",
                  {"amount_cents": 1000}, alice)
    assert forged.status_code == 404
    # Limpieza: quitar el budget de Bob para no afectar a otras suites.
    with _admin(TEST_DB) as c:
        c.execute(
            f"DELETE FROM budgets WHERE tenant_id = '{TENANT_A}' "
            f"AND level = 'user' AND user_id = '{USER_BOB}'"
        )


def test_parent_budget_below_its_allocations_needs_force(client, idp, admin_conn,
                                                         tenant_a_budget_state):
    """REQ-BUD-002/003 seen from above (live pilot 2026-11-15: lowering the org budget left a
    negative "unallocated" without a word). Lowering a parent below what it already handed out
    to the level below is the admin's call — an emergency cut must stay possible — so, like a
    budget below the spend, it needs force=true and is audited."""
    alice = mint(client, idp, "alice")
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM budgets WHERE tenant_id = %s "
            "AND level = 'dept' AND status = 'active'", (TENANT_A,))
        depts = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM budgets WHERE tenant_id = %s "
            "AND level = 'user' AND status = 'active' AND department_id = %s",
            (TENANT_A, DEPT_A1))
        a1_users = int(cur.fetchone()[0])
    assert depts > 1 and a1_users > 1  # the seed hands out budget at both levels

    def forced():
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT payload->>'level', (payload->>'allocated_cents')::bigint, "
                "(payload->>'amount_cents')::bigint FROM audit_log WHERE tenant_id = %s "
                "AND action = 'budget.forced_below_allocations' ORDER BY ts", (TENANT_A,))
            return cur.fetchall()

    before = len(forced())

    # Organization: without force → 422 with the excess and how to confirm it.
    low_org = _put(client, "/api/budgets/org", {"amount_cents": depts - 1}, alice)
    assert low_org.status_code == 422, low_org.text
    message = low_org.json()["error"]["message"]
    assert f"Department budgets add up to {depts} cents, 1 cents more" in message
    assert "force" in message  # the console opens its confirmation on this
    # Exactly what is allocated needs no confirmation.
    exact_org = _put(client, "/api/budgets/org", {"amount_cents": depts}, alice)
    assert exact_org.status_code == 200, exact_org.text
    assert len(forced()) == before
    # With force: accepted (an emergency cut stays possible) and audited.
    forced_org = _put(client, "/api/budgets/org", {"amount_cents": depts - 1, "force": True}, alice)
    assert forced_org.status_code == 200, forced_org.text
    assert forced()[before:] == [("org", depts, depts - 1)]
    overview = client.get("/api/budgets/overview", headers=bearer_headers(alice))
    assert overview.status_code == 200, overview.text
    assert overview.json()["unassigned_cents"] == -1  # the console words it, never "$-0.01"

    # Department: the same rule against the budgets of its people.
    low_dept = _put(client, f"/api/budgets/dept/{DEPT_A1}", {"amount_cents": a1_users - 1}, alice)
    assert low_dept.status_code == 422, low_dept.text
    assert f"User budgets of the department add up to {a1_users} cents" in low_dept.json()["error"]["message"]
    exact_dept = _put(client, f"/api/budgets/dept/{DEPT_A1}", {"amount_cents": a1_users}, alice)
    assert exact_dept.status_code == 200, exact_dept.text
    forced_dept = _put(client, f"/api/budgets/dept/{DEPT_A1}",
                       {"amount_cents": a1_users - 1, "force": True}, alice)
    assert forced_dept.status_code == 200, forced_dept.text
    assert forced()[before:] == [("org", depts, depts - 1), ("dept", a1_users, a1_users - 1)]
    # tenant_a_budget_state restores the org and A1 budgets of the seed.


def test_force_below_spend(client, idp, admin_conn, tenant_a_budget_state):
    """AC-GOV-003: reducir bajo el gasto → 422 sin force; con force → 200 +
    audit budget.forced_below_spend."""
    alice = mint(client, idp, "alice")
    # Gasto REAL en la ventana vigente para Carol (ledger + counters, como el
    # worker): el chequeo lee el ledger; un counter escrito a mano ya no cuenta.
    ingest_spend("acct-force-below-1", USER_CAROL, 50000)
    resp = _put(client, f"/api/budgets/user/{USER_CAROL}",
                {"amount_cents": 40000}, alice)
    assert resp.status_code == 422
    assert "force" in resp.json()["error"]["message"]
    forced = _put(client, f"/api/budgets/user/{USER_CAROL}",
                  {"amount_cents": 40000, "force": True}, alice)
    assert forced.status_code == 200, forced.text
    with _admin(TEST_DB) as c:
        row = c.execute(
            "SELECT count(*) FROM audit_log "
            f"WHERE tenant_id = '{TENANT_A}' AND action = 'budget.forced_below_spend'"
        ).fetchone()
    assert row[0] >= 1
    # tenant_a_budget_state restaura el presupuesto de Carol, los counters y
    # borra la fila de ledger del test.


def test_overview_unassigned_remainder(client, idp):
    """AC-GOV-004: remanente no asignado = org − Σ deptos (verificado)."""
    alice = mint(client, idp, "alice")
    resp = client.get("/api/budgets/overview", headers=bearer_headers(alice))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    org = data["organization"]["budget_cents"]
    dept_total = data["dept_budgets_total_cents"]
    assert data["unassigned_cents"] == org - dept_total
    assert len(data["departments"]) == 2
    dept = next(d for d in data["departments"] if d["name"] == "IT")
    assert dept["members"] >= 2  # Alice, Carol (y Frank) están en IT


def test_overview_lists_user_budgets_per_department(client, idp):
    """t_6b40c758 (QA F3): el overview anida los presupuestos por usuario
    bajo cada depto — vía de lectura para la UI de edición (PUT
    /api/budgets/user/{id}) — con Σ usuarios del depto (REQ-BUD-003)."""
    alice = mint(client, idp, "alice")
    resp = client.get("/api/budgets/overview", headers=bearer_headers(alice))
    assert resp.status_code == 200, resp.text
    departments = resp.json()["departments"]
    it = next(d for d in departments if d["name"] == "IT")
    # Seed: Carol $1,000 + Alice $2,000 (usuarios de IT); Frank sin budget.
    carol = next(u for u in it["users"] if u["user_id"] == USER_CAROL)
    alice_row = next(u for u in it["users"] if u["user_id"] == USER_ALICE)
    frank = next(u for u in it["users"] if u["email"] == "frank@tenant-a.test")
    assert carol["budget_cents"] == 100000
    assert alice_row["budget_cents"] == 200000
    assert frank["budget_cents"] is None
    assert frank["spent_cents"] == 0
    assert it["user_budgets_total_cents"] == 300000
    assert it["members"] == len(it["users"]) == 3
    # Σ usuarios nunca cuenta deptos ajenos: Finance no tiene user budgets.
    finance = next(d for d in departments if d["name"] == "Finance")
    assert finance["user_budgets_total_cents"] == 0
    assert all(u["budget_cents"] is None for u in finance["users"])
    # dept_admin: el scope del router filtra deptos; los usuarios anidados
    # viajan dentro del depto visible (Bob ve solo Finance, sin budgets).
    bob = mint(client, idp, "bob")
    scoped = client.get("/api/budgets/overview", headers=bearer_headers(bob))
    assert scoped.status_code == 200
    scoped_depts = scoped.json()["departments"]
    assert [d["department_id"] for d in scoped_depts] == [DEPT_A2]
    assert all(u["user_id"] != USER_CAROL for u in scoped_depts[0]["users"])


def test_overview_rbac_and_scope(client, idp):
    """AC-GOV-060: user → 403; dept_admin ve solo su depto."""
    carol = mint(client, idp, "carol")
    bob = mint(client, idp, "bob")
    denied = client.get("/api/budgets/overview", headers=bearer_headers(carol))
    assert denied.status_code == 403
    scoped = client.get("/api/budgets/overview", headers=bearer_headers(bob))
    assert scoped.status_code == 200
    assert all(d["department_id"] == DEPT_A2
               for d in scoped.json()["departments"])


def test_overview_lists_departments_without_budget(client, idp, admin_conn):
    """AC-013-ORG (REQ-ORG-001, spec 013 D-013-07): un depto recién creado
    aparece en el overview con budget_cents null y spent 0; tras
    PUT /api/budgets/dept/{id} muestra el monto asignado."""
    alice = mint(client, idp, "alice")
    created = client.post("/api/departments", json={"name": "Design"},
                          headers=bearer_headers(alice))
    assert created.status_code == 201, created.text
    dept_id = created.json()["department_id"]

    resp = client.get("/api/budgets/overview", headers=bearer_headers(alice))
    assert resp.status_code == 200, resp.text
    departments = resp.json()["departments"]
    fresh = next(d for d in departments if d["department_id"] == dept_id)
    assert fresh["budget_cents"] is None
    assert fresh["spent_cents"] == 0
    assert fresh["name"] == "Design"
    # El remanente no asignado no cuenta deptos sin presupuesto.
    org = resp.json()["organization"]["budget_cents"]
    total = resp.json()["dept_budgets_total_cents"]
    assert resp.json()["unassigned_cents"] == org - total

    # Tras asignarle presupuesto, el overview muestra el monto.
    assigned = _put(client, f"/api/budgets/dept/{dept_id}",
                    {"amount_cents": 50_000}, alice)
    assert assigned.status_code == 200, assigned.text
    resp = client.get("/api/budgets/overview", headers=bearer_headers(alice))
    fresh = next(d for d in resp.json()["departments"]
                 if d["department_id"] == dept_id)
    assert fresh["budget_cents"] == 50_000

    # Limpieza: eliminar el depto del test (sin miembros ni gasto).
    with admin_conn.cursor() as cur:
        cur.execute("DELETE FROM budgets WHERE department_id = %s",
                    (dept_id,))
        cur.execute("DELETE FROM departments WHERE id = %s", (dept_id,))
    admin_conn.commit()


def test_dept_admin_cannot_set_other_dept(client, idp):
    """AC-GOV-060: dept_admin fuera de su depto → 404 (sin oráculo)."""
    bob = mint(client, idp, "bob")
    resp = _put(client, f"/api/budgets/dept/{DEPT_A1}",
                {"amount_cents": 1000}, bob)
    assert resp.status_code == 404
    cross = _put(client, f"/api/budgets/dept/{DEPT_B1}",
                 {"amount_cents": 1000}, bob)
    assert cross.status_code == 404


def test_my_budget_status(client, idp):
    """AC-GOV-012: /api/budgets/me refleja los tres niveles."""
    carol = mint(client, idp, "carol")
    resp = client.get("/api/me/budget", headers=bearer_headers(carol))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["user"]["budget_cents"] == 100000
    assert data["department"]["budget_cents"] == 400000
    assert data["organization"]["budget_cents"] == 1000000
    assert data["cutoff"] is False
    assert data["user"]["remaining_cents"] == \
        data["user"]["budget_cents"] - data["user"]["spent_cents"]


def test_my_budget_status_explicit_zero_budget_cutoff(client, idp, admin_conn,
                                                     tenant_a_budget_state):
    """AC-GOV-012a (borde amount=0, gap B-1 t_ac2d2611): un presupuesto
    EXPLÍCITO de 0 centavos es un presupuesto vigente — la ruta de lectura
    aplica la misma regla que el enforcement (spent >= amount, sin guard
    `> 0`). Con spend ≥ 1¢ y budget 0, /api/budgets/me reporta
    cutoff=true, cutoff_level="user" (antes: remaining=-1, cutoff=false).
    Un presupuesto AUSENTE (sin fila) sigue sin corte."""
    alice = mint(client, idp, "alice")
    carol = mint(client, idp, "carol")

    # Gasto real en la ventana vigente para Carol (1¢, como el repro E2E).
    ingest_spend("acct-zero-budget-1", USER_CAROL, 1)
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(cost_billed_cents), 0) FROM spend_ledger "
            "WHERE tenant_id = %s AND user_id = %s "
            "AND occurred_at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",
            (TENANT_A, USER_CAROL))
        spent = cur.fetchone()[0]
    assert spent >= 1

    # Budget explícito 0 (force: 0 < gasto ya incurrido).
    zeroed = _put(client, f"/api/budgets/user/{USER_CAROL}",
                  {"amount_cents": 0, "force": True}, alice)
    assert zeroed.status_code == 200, zeroed.text

    # La ruta de lectura coincide con el enforcement: cortado.
    me = client.get("/api/budgets/me", headers=bearer_headers(carol))
    assert me.status_code == 200, me.text
    data = me.json()
    assert data["user"]["budget_cents"] == 0
    # Decimal(str(...)): the JSON carries sub-cent spend as a float.
    assert Decimal(str(data["user"]["spent_cents"])) == spent
    assert Decimal(str(data["user"]["remaining_cents"])) == -spent
    assert data["user"]["cutoff"] is True
    assert data["cutoff"] is True
    assert data["cutoff_level"] == "user"

    # La misma respuesta por el alias /api/me/budget (mismo servicio).
    alias = client.get("/api/me/budget", headers=bearer_headers(carol))
    assert alias.status_code == 200
    assert alias.json()["cutoff_level"] == "user"

    # Presupuesto ausente ≠ presupuesto 0: Dave (tenant B, sin ningún
    # budget) no reporta corte — budget_cents null, cutoff false.
    dave = mint(client, idp, "dave", tenant=TENANT_B)
    me_dave = client.get("/api/budgets/me",
                         headers=bearer_headers(dave))
    assert me_dave.status_code == 200, me_dave.text
    ddata = me_dave.json()
    assert ddata["user"]["budget_cents"] is None
    assert ddata["user"]["cutoff"] is False
    assert ddata["cutoff"] is False
    assert ddata["cutoff_level"] is None
    # tenant_a_budget_state restaura a Carol ($1,000, seed) y los counters.
