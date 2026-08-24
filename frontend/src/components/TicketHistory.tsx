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

/**
 * The audit log. One row per ticket, both legs together.
 *
 * Rejected and missed rows are shown, not hidden: "the bot attempted 400
 * tickets and filled 3" is the most useful thing this table can say, and a
 * missed ticket is how often an edge vanished between detection and
 * submission.
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
  });

  const rows = tickets.data ?? [];

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
            <th>Time</th>
            <th>Event</th>
            <th>Src</th>
            <th>Legs</th>
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
          ) : rows.length === 0 ? (
            <tr>
              <td colSpan={8} style={{ opacity: 0.5 }}>
                No tickets yet. An empty log is the expected state — measured
                2026-08-22, 0 of 245 groups had a net-positive pair.
              </td>
            </tr>
          ) : null}
          {rows.map((t) => {
            const dim = t.status !== "filled";
            return (
              <tr key={t.id} style={{ opacity: dim ? 0.55 : 1 }}>
                <td className="vt-mono" style={{ whiteSpace: "nowrap" }}>
                  {new Date(t.submitted_at).toLocaleTimeString("en-US", {
                    hour12: false,
                  })}
                </td>
                <td title={t.event_group_id}>{t.title_snapshot}</td>
                <td>
                  <span className={`tag ${t.source === "auto" ? "tag-accent" : "tag-outline"}`}>
                    {t.source}
                  </span>
                </td>
                <td>
                  {t.legs.length === 0 ? (
                    <span style={{ opacity: 0.6, fontSize: 11 }}>none submitted</span>
                  ) : (
                    t.legs.map((leg) => (
                      <LegLine key={`${leg.venue_id}:${leg.outcome_id}`} leg={leg} />
                    ))
                  )}
                </td>
                <td className="vt-mono">{money(t.total_stake)}</td>
                <td className="vt-mono">{money(t.expected_profit, { sign: true })}</td>
                <td
                  className="vt-mono"
                  style={{
                    color:
                      t.realized_profit === null
                        ? undefined
                        : Number(t.realized_profit) >= 0
                          ? "var(--vt-green-dark)"
                          : "var(--vt-red-dark)",
                  }}
                >
                  {t.realized_profit === null ? "open" : money(t.realized_profit, { sign: true })}
                </td>
                <td>
                  <span className="tag" title={t.rejection_reason ?? undefined}>
                    {t.status}
                  </span>
                  {t.rejection_reason ? (
                    <div style={{ fontSize: 10, opacity: 0.7, maxWidth: 220 }}>
                      {t.rejection_reason}
                    </div>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
