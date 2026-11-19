/**
 * Confirmación «force» de un presupuesto. PUT /api/budgets/{org|dept/{id}|user/{id}} responde
 * 422 cuando el nuevo límite queda por debajo de algo ya comprometido y el admin debe
 * confirmarlo con force=true (la decisión queda en la auditoría):
 *   - el gasto ya registrado en la ventana (REQ-GOV-003, budget.forced_below_spend);
 *   - lo ya asignado al nivel de abajo — departamentos de la organización o personas de un
 *     departamento (spec 005, evolución 0.7.4: budget.forced_below_allocations).
 * Las tres pantallas que editan presupuestos (Budgets, Overview, Departments) usan este copy.
 */

export type BudgetForceReason = "spend" | "allocations";

/** Qué compromiso confirma el admin, o null si el 422 no se resuelve con force. */
export function budgetForceReason(status: number, message: string): BudgetForceReason | null {
  if (status !== 422 || !/force/i.test(message)) return null;
  return /add up to/i.test(message) ? "allocations" : "spend";
}

export const BUDGET_FORCE_COPY: Record<BudgetForceReason, { title: string; message: string }> = {
  spend: {
    title: "Set budget below current spend?",
    message:
      "This level has already spent more than the new limit in the current period. Forcing it can cut off spending at this level right away. The decision is recorded in the audit log.",
  },
  allocations: {
    title: "Set budget below what is already allocated?",
    message:
      "The budgets handed out below this level add up to more than the new limit. They are kept, but this limit applies first: spending can be cut off here before they are used up. The decision is recorded in the audit log.",
  },
};
