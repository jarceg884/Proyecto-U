"""Budget accounting stays equal to the ledger (follow-up to the live budget audit of 15-nov-2026).

The live audit of the pilot found the cutoff itself correct but four accounting gaps, each pinned here:
- the overview reported ``spent_cents: 0`` for a user or department WITHOUT its own budget, although
  ingestion counts their spend (the console showed "Used $0.00" for a user who had spent dollars);
- a budget created with a weekly/daily period — or a period change — started its window at 0 even
  when the window already had spend (counters only accumulate the window in force when spend is
  ingested), so the force-below-spend check and the cutoff came late;
- ``budget_counters.budget_cents`` kept the value of the window's first spend, so the closed-period
  history could show a $0 budget;
- an amount-only edit (what the console sends) reset the period to monthly and the soft threshold
  to 80 %.

The reseed is also checked against the spend worker's real concurrency: an ingestion in flight when
an admin edits the budget must not be lost.
"""
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text

from conftest import (
    DEPT_A1, DEPT_A2, TENANT_A, TEST_DB, USER_BOB, USER_CAROL,
    _admin, bearer_headers, ingest_spend, mint,
)

MODEL = "glm-5.2"


def _ingest(request_id, user_id, cost_cents):
    ingest_spend(request_id, user_id, cost_cents, model=MODEL)


def _ledger_sum(admin_conn, column, entity, period):
    from app.governance.budgets import period_window, today

    start, end = period_window(period, today())
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(cost_billed_cents), 0) FROM spend_ledger "
            f"WHERE tenant_id = %s AND {column} = %s "
            "AND (occurred_at AT TIME ZONE 'UTC')::date BETWEEN %s AND %s",
            (TENANT_A, entity, start, end))
        return Decimal(cur.fetchone()[0])


def _counter(admin_conn, level, entity, period):
    from app.governance.budgets import period_window, today

    start, end = period_window(period, today())
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT spent_cents, budget_cents, period_end FROM budget_counters "
            "WHERE tenant_id = %s AND level = %s AND entity_id = %s AND period_start = %s",
            (TENANT_A, level, entity, start))
        row = cur.fetchone()
    return row, end


def _put(client, path, body, tokens):
    return client.put(path, json=body, headers=bearer_headers(tokens))


def test_overview_reports_monthly_spend_without_own_budget(client, idp, admin_conn,
                                                           tenant_a_budget_state):
    """Bob (Finance) has no user budget: the overview shows what he spent this month, not 0."""
    alice = mint(client, idp, "alice")

    def bob_row():
        resp = client.get("/api/budgets/overview", headers=bearer_headers(alice))
        assert resp.status_code == 200, resp.text
        finance = next(d for d in resp.json()["departments"] if d["department_id"] == DEPT_A2)
        return next(u for u in finance["users"] if u["user_id"] == USER_BOB)

    before = bob_row()
    assert before["budget_cents"] is None and before["period"] is None
    _ingest("acct-bob-1", USER_BOB, 250)
    after = bob_row()
    assert Decimal(str(after["spent_cents"])) == Decimal(str(before["spent_cents"])) + 250
    assert Decimal(str(after["spent_cents"])) == _ledger_sum(admin_conn, "user_id", USER_BOB, "monthly")


def test_weekly_budget_counts_spend_already_in_the_window(client, idp, admin_conn,
                                                          tenant_a_budget_state):
    """Switching Carol's monthly budget to weekly: the week's spend counts from the first moment."""
    alice = mint(client, idp, "alice")
    carol = mint(client, idp, "carol")
    _ingest("acct-carol-week-1", USER_CAROL, 400)
    week_spent = _ledger_sum(admin_conn, "user_id", USER_CAROL, "weekly")
    assert week_spent >= 400

    # Below what the week already spent: needs force (the old counter-based check read the
    # not-yet-existing weekly counter as 0 and let it through).
    refused = _put(client, f"/api/budgets/user/{USER_CAROL}",
                   {"amount_cents": 100, "period": "weekly"}, alice)
    assert refused.status_code == 422, refused.text
    assert "force=true" in refused.text

    ok = _put(client, f"/api/budgets/user/{USER_CAROL}",
              {"amount_cents": 100000, "period": "weekly"}, alice)
    assert ok.status_code == 200, ok.text
    assert ok.json()["period"] == "weekly"

    mine = client.get("/api/budgets/me", headers=bearer_headers(carol))
    assert mine.status_code == 200, mine.text
    assert Decimal(str(mine.json()["user"]["spent_cents"])) == week_spent
    (spent, budget, period_end), week_end = _counter(admin_conn, "user", USER_CAROL, "weekly")
    assert (spent, budget, period_end) == (week_spent, 100000, week_end)


def test_counter_carries_the_budget_in_force(client, idp, admin_conn, tenant_a_budget_state):
    """budget_counters.budget_cents (read by the closed-period history) follows budget edits and
    ingestion, never the budget of the window's first spend."""
    alice = mint(client, idp, "alice")
    resp = _put(client, f"/api/budgets/dept/{DEPT_A2}", {"amount_cents": 250000}, alice)
    assert resp.status_code == 200, resp.text
    (spent, budget, _end), _ = _counter(admin_conn, "dept", DEPT_A2, "monthly")
    assert budget == 250000
    assert spent == _ledger_sum(admin_conn, "department_id", DEPT_A2, "monthly")

    # A stale value in the row (what every pre-fix window carried) is replaced by the next spend.
    with admin_conn.cursor() as cur:
        cur.execute(
            "UPDATE budget_counters SET budget_cents = 0 WHERE tenant_id = %s AND level = 'dept' "
            "AND entity_id = %s", (TENANT_A, DEPT_A2))
    _ingest("acct-bob-2", USER_BOB, 120)
    (spent, budget, _end), _ = _counter(admin_conn, "dept", DEPT_A2, "monthly")
    assert budget == 250000
    assert spent == _ledger_sum(admin_conn, "department_id", DEPT_A2, "monthly")


def test_amount_only_edit_keeps_period_and_threshold(client, idp, admin_conn,
                                                     tenant_a_budget_state):
    """What the console sends ({amount_cents, force}) changes the amount and nothing else."""
    alice = mint(client, idp, "alice")
    first = _put(client, "/api/budgets/org",
                 {"amount_cents": 1_000_000, "period": "weekly", "soft_threshold_pct": 90,
                  "force": True}, alice)
    assert first.status_code == 200, first.text
    second = _put(client, "/api/budgets/org", {"amount_cents": 1_100_000}, alice)
    assert second.status_code == 200, second.text
    assert second.json()["period"] == "weekly"
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT amount_cents, period, soft_threshold_pct FROM budgets "
            "WHERE tenant_id = %s AND level = 'org' AND status = 'active'", (TENANT_A,))
        assert cur.fetchone() == (1_100_000, "weekly", 90)
    unknown = _put(client, "/api/budgets/org", {"amount_cents": 1_100_000, "period": "yearly"},
                   alice)
    assert unknown.status_code == 422, unknown.text


def test_reseed_does_not_lose_an_inflight_ingestion(admin_conn, tenant_a_budget_state):
    """An admin edits Carol's budget while the spend worker is ingesting her spend: the reseed must
    wait for the ingestion and count it (summing the ledger before locking the counter lost it)."""
    from app.db import get_engine, set_tenant, tenant_tx
    from app.governance import budgets, spend

    engine = get_engine()
    now = datetime.now(timezone.utc)
    worker = engine.connect()
    worker_tx = worker.begin()
    reseed_pid = {}
    result = {}
    try:
        set_tenant(worker, TENANT_A)
        # Exactly what ingest_entries writes for one entry, left uncommitted.
        worker.execute(
            text(
                "INSERT INTO spend_ledger (tenant_id, request_id, user_id, department_id, "
                "model_name, mode, cost_base_cents, cost_billed_cents, tokens_in, tokens_out, "
                "occurred_at) VALUES (CAST(:t AS uuid), 'acct-race-1', CAST(:u AS uuid), "
                "CAST(:d AS uuid), :m, 'central', 700, 700, 1, 1, :at)"
            ),
            {"t": TENANT_A, "u": USER_CAROL, "d": DEPT_A1, "m": MODEL, "at": now},
        )
        spend._bump_counters(worker, TENANT_A, user_id=USER_CAROL, department_id=DEPT_A1,
                             billed_cents=Decimal(700), when=now)

        def admin_edit():
            with tenant_tx(engine, TENANT_A) as conn:
                reseed_pid["pid"] = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
                result["spent"] = budgets._reseed_window_counter(
                    conn, level="user", entity_id=USER_CAROL, period="monthly",
                    amount_cents=100000)

        editor = threading.Thread(target=admin_edit)
        editor.start()
        with _admin(TEST_DB) as watcher:
            deadline = time.monotonic() + 15
            waiting = False
            while time.monotonic() < deadline and not waiting:
                pid = reseed_pid.get("pid")
                if pid is not None:
                    waiting = watcher.execute(
                        "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = %s",
                        (pid,)).fetchone() == (True,)
                if not waiting:
                    time.sleep(0.05)
        assert waiting, "the budget edit should wait for the in-flight ingestion"
        worker_tx.commit()
    finally:
        if worker_tx.is_active:
            worker_tx.rollback()
        worker.close()
    editor.join(timeout=15)
    assert not editor.is_alive()

    ledger = _ledger_sum(admin_conn, "user_id", USER_CAROL, "monthly")
    (counter, budget, _end), _ = _counter(admin_conn, "user", USER_CAROL, "monthly")
    assert result["spent"] == ledger == counter
    assert budget == 100000
