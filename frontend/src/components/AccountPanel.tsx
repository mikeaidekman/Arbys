import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { PaperAccountSummary, PnlSnapshot } from "../api/types";

const ACCOUNT = "default";
const SECTION_LABEL = {
  fontSize: 11,
  letterSpacing: "0.08em",
  textTransform: "uppercase" as const,
  opacity: 0.55,
};

function money(n: number, opts: { sign?: boolean } = {}): string {
  const s = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n >= 0 ? "+" : "-") : n < 0 ? "-" : "";
  return `${sign}$${s}`;
}

export function AccountPanel() {
  const summary = useQuery<PaperAccountSummary>({
    queryKey: ["paper", "summary", ACCOUNT],
    queryFn: () => api.paperSummary(ACCOUNT),
    refetchInterval: 5_000,
  });
  const pnl = useQuery<PnlSnapshot[]>({
    queryKey: ["paper", "pnl", ACCOUNT, "panel"],
    queryFn: () => api.paperPnl(ACCOUNT, 100),
    refetchInterval: 15_000,
  });

  const balances = summary.data?.balances ?? {};
  const realized = summary.data?.realized_pnl ?? {};
  const positions = summary.data?.positions ?? {};

  const cash = Object.values(balances).reduce((s, v) => s + Number(v), 0);
  const totalRealized = Object.values(realized).reduce((s, v) => s + Number(v), 0);
  const latest = pnl.data?.[pnl.data.length - 1];
  const equity = latest ? Number(latest.total_equity) : cash;
  const pnlValue = latest ? equity - cash + totalRealized : totalRealized;

  const openPositions = Object.entries(positions).filter(([, v]) => Number(v) !== 0);

  return (
    <aside
      className="vt-scroll"
      style={{
        borderLeft: "1px solid var(--color-divider)",
        padding: "var(--space-4)",
        overflowY: "auto",
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-3)",
      }}
    >
      <div style={SECTION_LABEL}>Account</div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: "var(--space-2)",
        }}
      >
        <div
          className="card"
          style={{ border: "1px solid var(--color-divider)", padding: "var(--space-2)" }}
        >
          <div className="card-kicker">Balance</div>
          <div className="vt-mono" style={{ fontSize: 19, fontWeight: 600 }}>
            {money(cash)}
          </div>
        </div>
        <div
          className="card"
          style={{ border: "1px solid var(--color-divider)", padding: "var(--space-2)" }}
        >
          <div className="card-kicker">P&amp;L</div>
          <div
            className="vt-mono"
            style={{
              fontSize: 19,
              fontWeight: 600,
              color: pnlValue >= 0 ? "var(--vt-green-dark)" : "#a1263c",
            }}
          >
            {money(pnlValue, { sign: true })}
          </div>
        </div>
      </div>

      <div style={{ ...SECTION_LABEL, marginTop: "var(--space-2)" }}>Venue balances</div>
      <table className="table" style={{ fontSize: 12 }}>
        <thead>
          <tr>
            <th>Venue</th>
            <th>Cash</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(balances).length === 0 ? (
            <tr>
              <td colSpan={3} style={{ opacity: 0.5 }}>
                No balances yet.
              </td>
            </tr>
          ) : null}
          {Object.entries(balances).map(([venue, amt]) => (
            <tr key={venue}>
              <td style={{ textTransform: "capitalize" }}>{venue}</td>
              <td className="vt-mono">${Number(amt).toFixed(2)}</td>
              <td>
                <span className="tag tag-accent">Connected</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div style={{ ...SECTION_LABEL, marginTop: "var(--space-2)" }}>Open positions</div>
      <table className="table" style={{ fontSize: 12 }}>
        <thead>
          <tr>
            <th>Outcome</th>
            <th>Qty</th>
          </tr>
        </thead>
        <tbody>
          {openPositions.length === 0 ? (
            <tr>
              <td colSpan={2} style={{ opacity: 0.5 }}>
                No open positions.
              </td>
            </tr>
          ) : null}
          {openPositions.map(([oid, qty]) => (
            <tr key={oid}>
              <td
                title={oid}
                style={{
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  maxWidth: 200,
                }}
              >
                {oid}
              </td>
              <td className="vt-mono">{Number(qty).toFixed(3)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </aside>
  );
}
