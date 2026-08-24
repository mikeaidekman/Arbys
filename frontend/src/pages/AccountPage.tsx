import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { PaperPosition, PnlSnapshot } from "../api/types";
import { AccountStrip } from "../components/AccountStrip";
import { TicketHistory } from "../components/TicketHistory";

const ACCOUNT = "default";

function money(v: string, opts: { sign?: boolean } = {}): string {
  const n = Number(v);
  const s = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n >= 0 ? "+" : "-") : n < 0 ? "-" : "";
  return `${sign}$${s}`;
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

  const open = (positions.data ?? []).filter((p) => Number(p.qty) !== 0);

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
          gap: "var(--space-5)",
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
                <th>Venue</th>
                <th>Qty</th>
                <th>Avg</th>
                <th>Mark</th>
                <th>Unrealized</th>
              </tr>
            </thead>
            <tbody>
              {open.length === 0 ? (
                <tr>
                  <td colSpan={6} style={{ opacity: 0.5 }}>
                    No open positions.
                  </td>
                </tr>
              ) : null}
              {open.map((p) => (
                <tr key={`${p.venue_id}:${p.outcome_id}`}>
                  <td title={p.outcome_id}>{p.title}</td>
                  <td style={{ textTransform: "capitalize" }}>
                    {p.venue_id.replace(/_/g, " ")}
                  </td>
                  <td className="vt-mono">{Number(p.qty).toFixed(2)}</td>
                  <td className="vt-mono">{(Number(p.avg_price) * 100).toFixed(1)}¢</td>
                  <td className="vt-mono">
                    {p.mark === null ? "—" : `${(Number(p.mark) * 100).toFixed(1)}¢`}
                  </td>
                  <td
                    className="vt-mono"
                    style={{
                      color:
                        Number(p.unrealized) >= 0
                          ? "var(--vt-green-dark)"
                          : "var(--vt-red-dark)",
                    }}
                  >
                    {money(p.unrealized, { sign: true })}
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
          <EquityCurve points={[...(pnl.data ?? [])].reverse()} />
        </section>
      </div>
    </div>
  );
}
