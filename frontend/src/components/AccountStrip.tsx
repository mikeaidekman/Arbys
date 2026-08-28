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
  primary,
}: {
  label: string;
  value: string;
  tone?: "pos" | "neg";
  /** The one figure the eye should land on first. Inverted to the darkest
   *  accent step so it reads as the anchor of the row rather than as the
   *  first of six equals. Only ever one tile. */
  primary?: boolean;
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 3,
        flex: "1 1 0",
        minWidth: 0,
        padding: "11px 13px",
        borderRadius: "var(--radius-md)",
        background: primary ? "var(--color-accent-800)" : "var(--color-surface)",
        border: `1px solid ${primary ? "var(--color-accent-800)" : "var(--color-divider)"}`,
      }}
    >
      <span
        style={{
          fontSize: 10,
          letterSpacing: "0.9px",
          textTransform: "uppercase",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
          // On the inverted tile a 0.55 opacity label goes muddy against the
          // navy, so it gets its own lighter step instead of a fade.
          color: primary ? "var(--color-accent-300)" : "var(--color-neutral-600)",
        }}
      >
        {label}
      </span>
      <span
        className="vt-mono"
        style={{
          fontSize: 21,
          fontWeight: 600,
          letterSpacing: "-0.5px",
          lineHeight: 1.15,
          color: primary
            ? "var(--color-surface)"
            : tone === "pos"
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

  // Tiles on the page ground rather than cells in a bordered band: the row
  // reads as six discrete objects, which is what lets the eye pick one out
  // instead of scanning a strip. No bottom rule — the gap between the tiles
  // and the table below is the separation.
  const stripStyle = {
    display: "flex",
    gap: "var(--space-2)",
    alignItems: "stretch",
    padding: "var(--space-3) var(--space-4)",
    flex: "none",
    flexWrap: "wrap",
  } as const;

  // This strip sits at the top of both / and /account, so a failed fetch is
  // the most visible thing on screen — render it as an error, not as a
  // quietly wiped $0.00 account (which is indistinguishable from a reset one).
  if (summary.isError) {
    return (
      <div style={stripStyle}>
        <span style={{ fontSize: 12, color: "var(--vt-red-dark)" }}>
          Couldn't load account summary
          {summary.error instanceof Error ? `: ${summary.error.message}` : "."}
        </span>
      </div>
    );
  }

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
    <div style={stripStyle}>
      <Cell label="Equity" value={money(equity)} primary />
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
