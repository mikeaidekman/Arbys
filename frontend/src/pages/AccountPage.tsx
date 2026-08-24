import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { PaperPosition, PnlSnapshot } from "../api/types";
import { AccountStrip } from "../components/AccountStrip";
import { TicketHistory } from "../components/TicketHistory";

const ACCOUNT = "default";

function amount(n: number, opts: { sign?: boolean } = {}): string {
  const s = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n >= 0 ? "+" : "-") : n < 0 ? "-" : "";
  return `${sign}$${s}`;
}

interface PositionEventRow {
  key: string;
  title: string;
  eventGroupId: string | null;
  legs: PaperPosition[];
  /** Cost basis: Σ qty × avg_price. */
  capital: number;
  /** Σ qty × mark, falling back to avg_price for an unquoted leg. */
  markValue: number;
  unrealized: number;
  unmarkedLegs: number;
}

/**
 * Collapse position legs into one row per event group.
 *
 * Keyed on `event_group_id`, never on the title string: one game's two legs
 * can resolve their titles from different sources — a ticket's frozen
 * snapshot on one, the live event_group join on the other — so a renamed
 * group would split a single game into two rows. A leg with no group id
 * (never traded through a ticket, and its group since retired) stands alone
 * under its own outcome id rather than being merged by name.
 *
 * `markValue` uses each leg's own mark where the venue is quoting and its
 * `avg_price` where it is not — flat mark-to-market, matching the backend's
 * `account_equity`, because a missing quote means unknown rather than
 * worthless. The count of unquoted legs is surfaced so the figure is not
 * mistaken for fully-marked.
 */
function groupPositionsByEvent(legs: PaperPosition[]): PositionEventRow[] {
  const rows = new Map<string, PositionEventRow>();
  for (const p of legs) {
    const key = p.event_group_id ?? `outcome:${p.outcome_id}`;
    let row = rows.get(key);
    if (row === undefined) {
      row = {
        key,
        title: p.title,
        eventGroupId: p.event_group_id,
        legs: [],
        capital: 0,
        markValue: 0,
        unrealized: 0,
        unmarkedLegs: 0,
      };
      rows.set(key, row);
    }
    const qty = Number(p.qty);
    const avg = Number(p.avg_price);
    row.legs.push(p);
    row.capital += qty * avg;
    row.markValue += qty * (p.mark === null ? avg : Number(p.mark));
    row.unrealized += Number(p.unrealized);
    if (p.mark === null) row.unmarkedLegs += 1;
  }
  return [...rows.values()].sort((a, b) => b.capital - a.capital);
}

function EquityCurve({ points }: { points: PnlSnapshot[] }) {
  if (points.length < 2) {
    return (
      <div style={{ opacity: 0.5, fontSize: 12 }}>
        Not enough snapshots yet — one is written every 30 seconds.
      </div>
    );
  }
  const values = points.map((p) => Number(p.total_equity));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const path = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * 100;
      const y = 100 - ((v - min) / span) * 100;
      return `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
  return (
    <svg
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      style={{ width: "100%", height: 120, display: "block" }}
      role="img"
      aria-label="Equity over time"
    >
      <path
        d={path}
        fill="none"
        stroke="var(--color-accent)"
        strokeWidth={1}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

export function AccountPage() {
  const positions = useQuery<PaperPosition[]>({
    queryKey: ["paper", "positions", ACCOUNT],
    queryFn: () => api.paperPositions(ACCOUNT),
    refetchInterval: 10_000,
  });
  const pnl = useQuery<PnlSnapshot[]>({
    queryKey: ["paper", "pnl", ACCOUNT, "account-page"],
    queryFn: () => api.paperPnl(ACCOUNT, 200),
    refetchInterval: 30_000,
  });

  const open = groupPositionsByEvent(
    (positions.data ?? []).filter((p) => Number(p.qty) !== 0),
  );

  return (
    <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column" }}>
      <nav className="nav" style={{ borderBottom: "1px solid var(--color-divider)" }}>
        <span className="nav-brand">Vantage</span>
        <span style={{ flex: 1 }} />
        <a href="/" className="tag tag-outline" style={{ textDecoration: "none" }}>
          Terminal
        </a>
        <a href="/admin" className="tag tag-outline" style={{ textDecoration: "none" }}>
          Admin
        </a>
      </nav>

      <AccountStrip />

      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-6)",
          padding: "var(--space-4)",
        }}
      >
        <TicketHistory />

        <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          <h2 style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: 16 }}>
            Open positions
          </h2>
          <table className="table" style={{ fontSize: 12 }}>
            <thead>
              <tr>
                <th>Event</th>
                <th>Legs</th>
                <th>Capital</th>
                <th>Mark value</th>
                <th>Unrealized</th>
              </tr>
            </thead>
            <tbody>
              {positions.isLoading ? (
                <tr>
                  <td colSpan={5} style={{ opacity: 0.5 }}>
                    Loading…
                  </td>
                </tr>
              ) : positions.isError ? (
                <tr>
                  <td colSpan={5} style={{ color: "var(--vt-red-dark)" }}>
                    Couldn't load open positions
                    {positions.error instanceof Error ? `: ${positions.error.message}` : "."}
                  </td>
                </tr>
              ) : open.length === 0 ? (
                <tr>
                  <td colSpan={5} style={{ opacity: 0.5 }}>
                    No open positions.
                  </td>
                </tr>
              ) : null}
              {open.map((row) => (
                <tr key={row.key}>
                  <td title={row.eventGroupId ?? undefined}>{row.title}</td>
                  <td>
                    {row.legs.map((p) => (
                      <div
                        key={`${p.venue_id}:${p.outcome_id}`}
                        style={{ fontSize: 11, opacity: 0.85, whiteSpace: "nowrap" }}
                        title={p.outcome_id}
                      >
                        <span style={{ textTransform: "capitalize" }}>
                          {p.venue_id.replace(/_/g, " ")}
                        </span>{" "}
                        {Number(p.qty).toFixed(2)} @{" "}
                        {(Number(p.avg_price) * 100).toFixed(1)}¢ → mark{" "}
                        {p.mark === null ? "—" : `${(Number(p.mark) * 100).toFixed(1)}¢`}
                      </div>
                    ))}
                  </td>
                  <td className="vt-mono">{amount(row.capital)}</td>
                  <td className="vt-mono">
                    {amount(row.markValue)}
                    {row.unmarkedLegs > 0 ? (
                      <div style={{ fontSize: 10, opacity: 0.7 }}>
                        {row.unmarkedLegs} leg{row.unmarkedLegs === 1 ? "" : "s"} unquoted
                      </div>
                    ) : null}
                  </td>
                  <td
                    className="vt-mono"
                    style={{
                      color:
                        row.unrealized >= 0
                          ? "var(--vt-green-dark)"
                          : "var(--vt-red-dark)",
                    }}
                  >
                    {amount(row.unrealized, { sign: true })}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          <h2 style={{ margin: 0, fontFamily: "var(--font-heading)", fontSize: 16 }}>
            Equity
          </h2>
          {pnl.isLoading ? (
            <div style={{ opacity: 0.5, fontSize: 12 }}>Loading…</div>
          ) : pnl.isError ? (
            <div style={{ fontSize: 12, color: "var(--vt-red-dark)" }}>
              Couldn't load equity history
              {pnl.error instanceof Error ? `: ${pnl.error.message}` : "."}
            </div>
          ) : (
            <EquityCurve points={[...(pnl.data ?? [])].reverse()} />
          )}
        </section>
      </div>
    </div>
  );
}
