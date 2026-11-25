// Budget «force» confirmation: which 422 messages of PUT /api/budgets/* the console offers to
// confirm, and with which copy (node --test, no extra deps). Run: npm run test:budget-force
// The messages below are the ones the control plane returns (platform/app/governance/budgets.py,
// pinned in platform/tests/test_budgets.py).
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { BUDGET_FORCE_COPY, budgetForceReason } from "../src/lib/budget-force.ts";

describe("budgetForceReason", () => {
  it("offers to confirm a budget below the spend already recorded", () => {
    const message =
      "Budget (200 cents) is below the spend already incurred this period (290.5 cents). Pass force=true to confirm (the decision is audited).";
    assert.equal(budgetForceReason(422, message), "spend");
  });

  it("offers to confirm a budget below what is already allocated", () => {
    const org =
      "Department budgets add up to 1000 cents, 200 cents more than the new organization budget (800 cents). Lower the department budgets first, or pass force=true to confirm (the decision is audited).";
    const dept =
      "User budgets of the department add up to 300000 cents, 1 cents more than the new department budget (299999 cents). Lower those user budgets first, or pass force=true to confirm (the decision is audited).";
    assert.equal(budgetForceReason(422, org), "allocations");
    assert.equal(budgetForceReason(422, dept), "allocations");
  });

  it("does not offer force for rules that force cannot override", () => {
    // Σ departments above the organization (set from below) is a hard rule.
    const hard =
      "Department budgets would exceed the organization budget by 1 cents (Σ departments = 1000001 > org = 1000000).";
    assert.equal(budgetForceReason(422, hard), null);
    assert.equal(budgetForceReason(403, "Pass force=true"), null);
  });

  it("explains each reason in plain words, without API jargon", () => {
    for (const copy of Object.values(BUDGET_FORCE_COPY)) {
      assert.ok(copy.title.endsWith("?"));
      assert.match(copy.message, /audit log/);
      assert.doesNotMatch(copy.message, /force=true|cents/);
    }
  });
});
