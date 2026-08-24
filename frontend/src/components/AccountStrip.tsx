import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { PaperAccountSummary } from "../api/types";

const ACCOUNT = "default";

function money(n: number, opts: { sign?: boolean } = {}): string {
  const s = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n >= 0 ? "+" : "-") : n < 0 ? "-" : "";
  return `${sign}$${s}`;
}

function Cell({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "pos" | "neg";
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <span
        style={{
          fontSize: 10,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          opacity: 0.55,
        }}
      >
        {label}
      </span>
      <span
        className="vt-mono"
        style={{
          fontSize: 15,
          fontWeight: 600,
          color:
            tone === "pos"
              ? "var(--vt-green-dark)"
              : tone === "neg"
                ? "var(--vt-red-dark)"
                : "var(--color-text)",
        }}
      >
        {value}
      </span>
    </div>
  );
}

/**
 * The account summary that replaces the old right-hand sidebar. Reused as the
 * header of /account so the two views cannot drift apart.
 *
 * Figures come from the live summary endpoint, not from pnl_snapshots: those
 * are written every 30s and do not exist at all until the first one lands
 * after a restart.
 */
export function AccountStrip() {
  const summary = useQuery<PaperAccountSummary>({
    queryKey: ["paper", "summary", ACCOUNT],
    queryFn: () => api.paperSummary(ACCOUNT),
    refetchInterval: 5_000,
  });

  const cash = Number(summary.data?.cash ?? 0);
  const positionValue = Number(summary.data?.position_value ?? 0);
  const equity = Number(summary.data?.equity ?? 0);
  const unrealized = Number(summary.data?.unrealized_pnl ?? 0);
  const realized = Object.values(summary.data?.realized_pnl ?? {}).reduce(
    (s, v) => s + Number(v),
    0,
  );
  const openTickets = summary.data?.open_ticket_count ?? 0;

  const tone = (n: number) => (n > 0 ? "pos" : n < 0 ? "neg" : undefined);

  return (
    <div
      style={{
        display: "flex",
        gap: "var(--space-4)",
        alignItems: "center",
        padding: "var(--space-2) var(--space-4)",
        borderBottom: "1px solid var(--color-divider)",
        flex: "none",
        flexWrap: "wrap",
      }}
    >
      <Cell label="Equity" value={money(equity)} />
      <Cell label="Cash" value={money(cash)} />
      <Cell label="Position value" value={money(positionValue)} />
      <Cell
        label="Unrealized"
        value={money(unrealized, { sign: true })}
        tone={tone(unrealized)}
      />
      <Cell
        label="Realized"
        value={money(realized, { sign: true })}
        tone={tone(realized)}
      />
      <Cell label="Open tickets" value={String(openTickets)} />
    </div>
  );
}
