import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Ticket, TicketLeg } from "../api/types";

const ACCOUNT = "default";

const STATUSES = ["all", "filled", "rejected", "missed"] as const;
const SOURCES = ["all", "manual", "auto"] as const;

function money(v: string | null, opts: { sign?: boolean } = {}): string {
  if (v === null) return "—";
  const n = Number(v);
  const s = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n >= 0 ? "+" : "-") : n < 0 ? "-" : "";
  return `${sign}$${s}`;
}

function cents(v: string | null): string {
  return v === null ? "—" : `${(Number(v) * 100).toFixed(1)}¢`;
}

/** Same formatting as `money`, for values already summed into a number. */
function amount(n: number | null, opts: { sign?: boolean } = {}): string {
  if (n === null) return "—";
  const s = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n >= 0 ? "+" : "-") : n < 0 ? "-" : "";
  return `${sign}$${s}`;
}

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-US", { hour12: false });
}

function LegLine({ leg }: { leg: TicketLeg }) {
  return (
    <div style={{ fontSize: 11, opacity: 0.85, whiteSpace: "nowrap" }}>
      <span style={{ textTransform: "capitalize" }}>
        {leg.venue_id.replace(/_/g, " ")}
      </span>{" "}
      {leg.is_buy ? "BUY" : "SELL"} {Number(leg.qty).toFixed(2)} @{" "}
      {cents(leg.fill_price ?? leg.limit_price)}
      {leg.fill_price === null ? " (unfilled)" : ""}
      {Number(leg.fee) > 0 ? ` · fee ${money(leg.fee)}` : ""}
    </div>
  );
}

/** One ticket inside an event row: its own economics, then its legs. */
function TicketLine({ ticket }: { ticket: Ticket }) {
  const realized =
    ticket.realized_profit !== null
      ? money(ticket.realized_profit, { sign: true })
      : ticket.status === "rejected" || ticket.status === "missed"
        ? "—"
        : "open";
  return (
    <div style={{ marginTop: 4 }}>
      <div style={{ fontSize: 11, whiteSpace: "nowrap" }}>
        <span className="vt-mono">{clock(ticket.submitted_at)}</span>{" "}
        <span className="tag" title={ticket.rejection_reason ?? undefined}>
          {ticket.status}
        </span>{" "}
        {money(ticket.total_stake)} → {realized}
      </div>
      {ticket.rejection_reason ? (
        <div style={{ fontSize: 10, opacity: 0.7, maxWidth: 260 }}>
          {ticket.rejection_reason}
        </div>
      ) : null}
      {ticket.legs.length === 0 ? (
        <div style={{ fontSize: 11, opacity: 0.6 }}>none submitted</div>
      ) : (
        ticket.legs.map((leg) => (
          <LegLine key={`${leg.venue_id}:${leg.outcome_id}`} leg={leg} />
        ))
      )}
    </div>
  );
}

interface EventRow {
  key: string;
  title: string;
  eventGroupId: string;
  tickets: Ticket[];
  latest: string;
  stake: number | null;
  expected: number | null;
  realized: number | null;
  /** Filled or pending tickets whose legs have not all settled yet. */
  openCount: number;
  statusCounts: Record<string, number>;
  sources: Set<string>;
}

/**
 * Collapse tickets into one row per event group.
 *
 * Grouped on `event_group_id`, never on `title_snapshot`: the snapshot is
 * frozen per ticket, so a group renamed between two fills would otherwise
 * split one game across two rows.
 *
 * Sums skip nulls rather than coercing them to zero — a missed ticket has no
 * economics, and `Number(null)` is 0, which would read as a free ticket that
 * made nothing. A column whose every contributor is null stays null so it
 * renders as an em dash.
 */
function groupByEvent(tickets: Ticket[]): EventRow[] {
  const rows = new Map<string, EventRow>();
  for (const t of tickets) {
    const key = t.event_group_id;
    let row = rows.get(key);
    if (row === undefined) {
      row = {
        key,
        title: t.title_snapshot,
        eventGroupId: t.event_group_id,
        tickets: [],
        latest: t.submitted_at,
        stake: null,
        expected: null,
        realized: null,
        openCount: 0,
        statusCounts: {},
        sources: new Set(),
      };
      rows.set(key, row);
    }
    row.tickets.push(t);
    row.sources.add(t.source);
    row.statusCounts[t.status] = (row.statusCounts[t.status] ?? 0) + 1;
    if (t.submitted_at > row.latest) {
      row.latest = t.submitted_at;
      // The newest ticket's snapshot is the most current name for the event.
      row.title = t.title_snapshot;
    }
    if (t.total_stake !== null) row.stake = (row.stake ?? 0) + Number(t.total_stake);
    if (t.expected_profit !== null)
      row.expected = (row.expected ?? 0) + Number(t.expected_profit);
    if (t.realized_profit !== null) {
      row.realized = (row.realized ?? 0) + Number(t.realized_profit);
    } else if (t.status === "filled" || t.status === "pending") {
      row.openCount += 1;
    }
  }
  return [...rows.values()].sort((a, b) => (a.latest < b.latest ? 1 : -1));
}

function summariseStatuses(counts: Record<string, number>): string {
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .map(([s, n]) => `${n} ${s}`)
    .join(" · ");
}

/**
 * The audit log, one row per event.
 *
 * Rejected and missed tickets are counted, not hidden: "the bot attempted 400
 * tickets and filled 3" is the most useful thing this table can say, and a
 * missed ticket is how often an edge vanished between detection and
 * submission. Each event's individual tickets stay visible as sub-lines with
 * their own legs and fill prices, because "what did I pay" is the question
 * this page exists to answer and an aggregate alone cannot answer it.
 */
export function TicketHistory() {
  const [status, setStatus] = useState<(typeof STATUSES)[number]>("all");
  const [source, setSource] = useState<(typeof SOURCES)[number]>("all");

  const tickets = useQuery<Ticket[]>({
    queryKey: ["paper", "tickets", ACCOUNT, status, source],
    queryFn: () =>
      api.paperTickets(ACCOUNT, {
        status: status === "all" ? undefined : status,
        source: source === "all" ? undefined : source,
      }),
    refetchInterval: 10_000,
    // Keep the previous rows visible across a filter change instead of
    // flashing back to the loading state — each filter combo is its own
    // cache entry, so without this every click would blank the table.
    placeholderData: (prev) => prev,
  });

  const rows = groupByEvent(tickets.data ?? []);

  return (
    <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
      <div style={{ display: "flex", gap: "var(--space-3)", alignItems: "center" }}>
        <h2 style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: 16 }}>
          Ticket history
        </h2>
        <span style={{ flex: 1 }} />
        {STATUSES.map((s) => (
          <button
            key={s}
            className={`btn ${status === s ? "btn-primary" : ""}`}
            style={{ fontSize: 11, padding: "2px 8px", textTransform: "capitalize" }}
            onClick={() => setStatus(s)}
          >
            {s}
          </button>
        ))}
        {SOURCES.map((s) => (
          <button
            key={s}
            className={`btn ${source === s ? "btn-primary" : ""}`}
            style={{ fontSize: 11, padding: "2px 8px", textTransform: "capitalize" }}
            onClick={() => setSource(s)}
          >
            {s}
          </button>
        ))}
      </div>

      <table className="table" style={{ fontSize: 12 }}>
        <thead>
          <tr>
            <th>Last</th>
            <th>Event</th>
            <th>Src</th>
            <th>Tickets</th>
            <th>Stake</th>
            <th>Expected</th>
            <th>Realized</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {tickets.isLoading ? (
            <tr>
              <td colSpan={8} style={{ opacity: 0.5 }}>
                Loading…
              </td>
            </tr>
          ) : tickets.isError ? (
            <tr>
              <td colSpan={8} style={{ color: "var(--vt-red-dark)" }}>
                Couldn't load ticket history
                {tickets.error instanceof Error ? `: ${tickets.error.message}` : "."}
              </td>
            </tr>
          ) : rows.length === 0 ? (
            <tr>
              <td colSpan={8} style={{ opacity: 0.5 }}>
                No tickets yet. An empty log is the expected state — measured
                2026-08-22, 0 of 245 groups had a net-positive pair.
              </td>
            </tr>
          ) : null}
          {rows.map((r) => {
            const dim = (r.statusCounts.filled ?? 0) === 0;
            return (
              <tr key={r.key} style={{ opacity: dim ? 0.55 : 1 }}>
                <td className="vt-mono" style={{ whiteSpace: "nowrap" }}>
                  {clock(r.latest)}
                </td>
                <td title={r.eventGroupId}>{r.title}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  {[...r.sources].sort().map((s) => (
                    <span
                      key={s}
                      className={`tag ${s === "auto" ? "tag-accent" : "tag-outline"}`}
                    >
                      {s}
                    </span>
                  ))}
                </td>
                <td>
                  <div style={{ fontSize: 11, opacity: 0.7 }}>
                    {r.tickets.length === 1 ? "1 ticket" : `${r.tickets.length} tickets`}
                  </div>
                  {r.tickets.map((t) => (
                    <TicketLine key={t.id} ticket={t} />
                  ))}
                </td>
                <td className="vt-mono">{amount(r.stake)}</td>
                <td className="vt-mono">{amount(r.expected, { sign: true })}</td>
                <td
                  className="vt-mono"
                  style={{
                    color:
                      r.realized === null
                        ? undefined
                        : r.realized >= 0
                          ? "var(--vt-green-dark)"
                          : "var(--vt-red-dark)",
                  }}
                >
                  {r.realized !== null ? amount(r.realized, { sign: true }) : r.openCount > 0 ? "open" : "—"}
                  {r.realized !== null && r.openCount > 0 ? (
                    <div style={{ fontSize: 10, opacity: 0.7 }}>
                      {r.openCount} still open
                    </div>
                  ) : null}
                </td>
                <td style={{ fontSize: 11 }}>{summariseStatuses(r.statusCounts)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
