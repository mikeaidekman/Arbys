import type {
  ArbOpportunity,
  EventGroup,
  MonitoredGroup,
  PaperAccountSummary,
  PaperOrder,
  PnlSnapshot,
} from "./types";

const BASE = "/api";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  listEventGroups: () => req<EventGroup[]>("/event-groups"),
  createEventGroup: (body: EventGroup) =>
    req<EventGroup>("/event-groups", { method: "POST", body: JSON.stringify(body) }),
  deleteEventGroup: (id: string) =>
    req<void>(`/event-groups/${id}`, { method: "DELETE" }),

  pushQuote: (outcome_id: string, bid: string, ask: string) =>
    req<void>("/quotes", {
      method: "POST",
      body: JSON.stringify({ outcome_id, bid, ask }),
    }),

  listOpportunities: (limit = 50) =>
    req<ArbOpportunity[]>(`/opportunities?limit=${limit}`),

  listMonitored: () => req<MonitoredGroup[]>("/monitored"),

  executeArb: (opportunity_index: number, account_id?: string) =>
    req<string[]>("/paper/execute", {
      method: "POST",
      body: JSON.stringify({ opportunity_index, account_id }),
    }),

  paperSummary: (account_id: string) =>
    req<PaperAccountSummary>(`/paper/${account_id}`),
  paperOrders: (account_id: string) =>
    req<PaperOrder[]>(`/paper/${account_id}/orders`),
  paperPnl: (account_id: string, limit = 500) =>
    req<PnlSnapshot[]>(`/paper/${account_id}/pnl-snapshots?limit=${limit}`),
};
