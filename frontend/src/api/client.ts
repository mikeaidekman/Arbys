import type {
  ArbOpportunity,
  EventGroup,
  MonitoredGroup,
  PaperAccountSummary,
  PaperOrder,
  PaperPosition,
  Performance,
  PnlSnapshot,
  TicketPage,
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

  /**
   * Fill an arb identified by its event group and buy legs. The server
   * resolves it against the live opportunity list at execution time.
   *
   * Deliberately not index-based: this client merges websocket-pushed
   * opportunities ahead of REST ones, so an index into our array does not
   * address the same entry in the server's list.
   */
  executeArb: (
    event_group_id: string,
    outcome_ids: string[],
    account_id?: string,
  ) =>
    req<string[]>("/paper/execute", {
      method: "POST",
      body: JSON.stringify({ event_group_id, outcome_ids, account_id }),
    }),

  paperSummary: (account_id: string) =>
    req<PaperAccountSummary>(`/paper/${account_id}`),
  paperOrders: (account_id: string) =>
    req<PaperOrder[]>(`/paper/${account_id}/orders`),
  paperPnl: (account_id: string, limit = 500) =>
    req<PnlSnapshot[]>(`/paper/${account_id}/pnl-snapshots?limit=${limit}`),
  paperReset: (account_id: string) =>
    req<PaperAccountSummary>(`/paper/${account_id}/reset`, { method: "POST" }),
  /**
   * One page of the ledger, newest first.
   *
   * Paged rather than capped: pass `cursor` from the previous page's
   * `next_cursor` to continue. `since` is an ISO instant — the client owns the
   * range because "Today" means the viewer's local midnight, which the server
   * cannot know.
   */
  paperTickets: (
    account_id: string,
    opts: {
      limit?: number;
      status?: string;
      source?: string;
      since?: string | null;
      outcome?: string | null;
      cursor?: string | null;
    } = {},
  ) => {
    const q = new URLSearchParams();
    q.set("limit", String(opts.limit ?? 100));
    if (opts.status) q.set("status", opts.status);
    if (opts.source) q.set("source", opts.source);
    if (opts.since) q.set("since", opts.since);
    if (opts.outcome) q.set("outcome", opts.outcome);
    if (opts.cursor) q.set("cursor", opts.cursor);
    return req<TicketPage>(`/paper/${account_id}/tickets?${q.toString()}`);
  },

  /** Dashboard figures over every ticket in the window, aggregated server-side. */
  paperPerformance: (account_id: string, since?: string | null) => {
    const q = new URLSearchParams();
    if (since) q.set("since", since);
    return req<Performance>(`/paper/${account_id}/performance?${q.toString()}`);
  },
  paperPositions: (account_id: string) =>
    req<PaperPosition[]>(`/paper/${account_id}/positions`),
};
