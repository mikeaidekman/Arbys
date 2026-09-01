import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { PaperAccountSummary, PnlSnapshot, Ticket } from "../api/types";
import { Logo } from "../components/Logo";
import {
  RANGES,
  settlementBuckets,
  summarize,
  toLedgerRow,
  type Dashboard,
  type LedgerRow,
  type Outcome,
  type SettlementBucket,
} from "../lib/performance";

const ACCOUNT = "default";

/** Well above the 200 default: a 90-day window must not silently truncate. */
const TICKET_LIMIT = 1000;

function amount(n: number | null, opts: { sign?: boolean } = {}): string {
  if (n === null) return "—";
  const s = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const sign = opts.sign ? (n >= 0 ? "+" : "-") : n < 0 ? "-" : "";
  return `${sign}$${s}`;
}

function round0(n: number | null): string {
  if (n === null) return "—";
  return `$${Math.round(Math.abs(n)).toLocaleString("en-US")}`;
}

function pct(n: number | null, digits = 1): string {
  return n === null ? "—" : `${n.toFixed(digits)}%`;
}

function cents(n: number | null, digits = 2): string {
  return n === null ? "—" : `${n.toFixed(digits)}¢`;
}

function day(iso: string): string {
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function money(n: number | null): string {
  return n === null ? "—" : amount(n);
}

/** Green for gain, red for loss, inherited for unknown — never green for null. */
function pnlColor(n: number | null): string {
  if (n === null) return "var(--color-text)";
  return n >= 0 ? "var(--vt-green-dark)" : "var(--vt-red-dark)";
}

function Kpi({
  label,
  value,
  sub,
  color,
  primary,
}: {
  label: string;
  value: string;
  sub: string;
  color?: string;
  /** The one tile the eye should land on. `color` is ignored when set: the
   *  inverted tile carries its own foreground, and a green/red pnlColor on
   *  navy is unreadable. */
  primary?: boolean;
}) {
  return (
    <div className={`vt-kpi${primary ? " vt-kpi-primary" : ""}`}>
      <div className="vt-lab">{label}</div>
      <div
        className="vt-mono"
        style={{
          fontSize: 22,
          fontWeight: 600,
          letterSpacing: "-0.5px",
          color: primary ? "var(--color-surface)" : (color ?? "var(--color-text)"),
        }}
      >
        {value}
      </div>
      <div
        style={{
          fontSize: 11,
          color: primary ? "var(--color-accent-300)" : "var(--color-neutral-600)",
        }}
      >
        {sub}
      </div>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 12, opacity: 0.5 }}>{children}</div>;
}

/**
 * Cumulative net P&L over the window.
 *
 * Plots equity change from the first snapshot in range, so it includes open
 * marks — which is what the snapshot series measures and what the "net profit"
 * tile above it reports. Unlike the artboard's version this handles a negative
 * excursion: an arb book can and does dip, and a chart that assumes monotonic
 * profit would clip the dip rather than draw it.
 */
function Bar({ value, max, color }: { value: number | null; max: number; color: string }) {
  const width = value === null || max <= 0 ? 0 : (Math.abs(value) / max) * 100;
  return (
    <div style={{ height: 10, border: "1px solid var(--color-divider)", position: "relative" }}>
      <div
        style={{
          position: "absolute",
          inset: "0 auto 0 0",
          width: `${Math.max(value === null ? 0 : 1.5, width).toFixed(1)}%`,
          background: color,
        }}
      />
    </div>
  );
}

function BreakdownPanel({
  label,
  rows,
  emptyNote,
}: {
  label: string;
  rows: Dashboard["byLeague"];
  emptyNote: string;
}) {
  const max = Math.max(...rows.map((r) => Math.abs(r.net ?? 0)), 0);
  return (
    <div className="vt-panel">
      <div className="vt-lab">{label}</div>
      {rows.length === 0 ? (
        <Empty>{emptyNote}</Empty>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
          {rows.map((r) => (
            <div key={r.name} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                <span style={{ textTransform: "uppercase" }}>{r.name}</span>
                <span className="vt-mono" style={{ opacity: 0.75, color: pnlColor(r.net) }}>
                  {r.net === null ? "unsettled" : amount(r.net, { sign: true })}
                </span>
              </div>
              <Bar
                value={r.net}
                max={max}
                color={(r.net ?? 0) >= 0 ? "var(--color-accent)" : "var(--vt-red-dark)"}
              />
              <div className="vt-mono" style={{ fontSize: 10, opacity: 0.5 }}>
                {r.tickets} ticket{r.tickets === 1 ? "" : "s"} · {round0(r.capital)} deployed
                {r.roi === null ? "" : ` · ${pct(r.roi)} ROI`}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const OUTCOME_LABEL: Record<Outcome, string> = {
  won: "won",
  lost: "lost",
  flat: "flat",
  open: "open",
  none: "—",
};

const OUTCOME_TAG: Record<Outcome, string> = {
  won: "tag-accent",
  lost: "tag-neutral",
  flat: "tag-neutral",
  open: "tag-outline",
  none: "tag-neutral",
};

const FILTERS: { key: string; match: (r: LedgerRow) => boolean }[] = [
  { key: "All", match: () => true },
  { key: "Open", match: (r) => r.outcome === "open" },
  { key: "Won", match: (r) => r.outcome === "won" },
  { key: "Lost", match: (r) => r.outcome === "lost" },
  { key: "Never filled", match: (r) => r.outcome === "none" },
];

export function AccountPage() {
  const [range, setRange] = useState<string>(RANGES[0].key);
  const [filter, setFilter] = useState<string>("All");

  const tickets = useQuery<Ticket[]>({
    queryKey: ["paper", "tickets", ACCOUNT, "performance"],
    queryFn: () => api.paperTickets(ACCOUNT, { limit: TICKET_LIMIT }),
    refetchInterval: 15_000,
  });
  const pnl = useQuery<PnlSnapshot[]>({
    queryKey: ["paper", "pnl", ACCOUNT, "performance"],
    queryFn: () => api.paperPnl(ACCOUNT, 1000),
    refetchInterval: 30_000,
  });
  // Balances only — the rest of this page is computed from the ticket ledger.
  // Cash headroom cannot be: it is what is LEFT, not what was spent.
  const summary = useQuery<PaperAccountSummary>({
    queryKey: ["paper", "summary", ACCOUNT, "performance"],
    queryFn: () => api.paperSummary(ACCOUNT),
    refetchInterval: 15_000,
  });

  const rangeOption = RANGES.find((r) => r.key === range) ?? RANGES[0];
  // Snapshots arrive newest-first; the curve reads left to right.
  const snapshots = [...(pnl.data ?? [])].reverse();
  const d = summarize(tickets.data ?? [], snapshots, rangeOption);

  const shown = d.rows.filter(
    (r) => FILTERS.find((f) => f.key === filter)?.match(r) ?? true,
  );
  // Null qty means the ticket has no legs — a miss or a pre-execution
  // rejection never traded — so it contributes 0 contracts rather than making
  // the total unknown. Same reasoning as capital below.
  const shownQty = shown.reduce((a, r) => a + (r.qty ?? 0), 0);
  const shownCapital = shown.reduce((a, r) => a + (r.capital ?? 0), 0);
  const shownReturned = shown.reduce((a, r) => a + (r.returned ?? 0), 0);
  const shownNet = shown.some((r) => r.net !== null)
    ? shown.reduce((a, r) => a + (r.net ?? 0), 0)
    : null;

  // Deliberately NOT windowed by `range`, unlike every other tile in the row.
  // A ticket is awaiting settlement now or it is not; asking what was pending
  // "in the last 7 days" has no meaning, and a ticket submitted before the
  // window opened is still owed to you.
  //
  // Computed from the ticket ledger rather than the positions endpoint, which
  // this page no longer queries at all. Positions are marked at mid, and a mark
  // is noise on a hedged book: the pair settles for $1 whoever wins, so the
  // profit was fixed at fill time and no quote can change it.
  const allRows = (tickets.data ?? []).map(toLedgerRow);
  const pendingRows = allRows.filter((r) => r.status === "filled" && r.net === null);
  const hedged = pendingRows.filter((r) => r.settlementValue !== null);
  // Legged: one side filled, the other not, or the two filled at different
  // sizes. It has no guaranteed payout, so it is excluded from every total on
  // this page and surfaced on its own instead. The per-leg positions table used
  // to be the only place this was visible; dropping it without this would have
  // hidden the one position type worth worrying about.
  const unhedged = pendingRows.filter((r) => r.settlementValue === null);
  const pendingValue = hedged.length
    ? hedged.reduce((a, r) => a + (r.settlementValue ?? 0), 0)
    : null;
  const pendingContracts = hedged.reduce((a, r) => a + (r.qty ?? 0), 0);
  const buckets = settlementBuckets(pendingRows);
  // Windowed, unlike the tiles above: this is the header for the accrual chart
  // and must agree with what the chart actually draws.
  const accrual = {
    total: d.rows.reduce((a, r) => a + (r.settlementValue ?? 0), 0),
  };

  const maxBucket = Math.max(...d.edgeBuckets.map((b) => b.count), 1);
  const loading = tickets.isLoading || pnl.isLoading;

  return (
    <div
      className="vt-dash"
      style={{ minHeight: "100vh", display: "flex", flexDirection: "column" }}
    >
      <nav
        className="nav"
        style={{
          borderBottom: "1px solid var(--color-divider)",
          flex: "none",
          position: "sticky",
          top: 0,
          zIndex: 5,
          // White header band over the grey page ground, matching the
          // terminal. It was --color-bg when page and card shared a colour;
          // now a sticky --color-bg nav would scroll as a grey stripe over
          // the white panels beneath it.
          background: "var(--color-surface)",
        }}
      >
        <Logo />
        <span style={{ display: "flex", gap: 2, marginLeft: "var(--space-4)" }}>
          <a className="vt-tab" href="/">
            Terminal
          </a>
          <span className="vt-tab vt-tab-on">Performance</span>
          <a className="vt-tab" href="/admin">
            Admin
          </a>
        </span>
        <span style={{ flex: 1 }} />
        <span
          style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, opacity: 0.6 }}
        >
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: "50%",
              background: "var(--color-accent)",
              animation: "vt-pulse 1.6s ease-in-out infinite",
            }}
          />
          {tickets.isFetching || pnl.isFetching ? "refreshing" : "live"}
        </span>
      </nav>

      <div
        style={{
          padding: "var(--space-4)",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-4)",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "flex-end",
            gap: "var(--space-4)",
            flexWrap: "wrap",
          }}
        >
          <div>
            <div className="vt-lab">Portfolio performance</div>
            <div
              style={{ fontFamily: "var(--font-heading)", fontSize: 26, letterSpacing: ".01em" }}
            >
              Realized arbitrage results
            </div>
          </div>
          <span style={{ flex: 1 }} />
          <div style={{ display: "flex", gap: 2 }}>
            {RANGES.map((r) => (
              <button
                key={r.key}
                type="button"
                className={`vt-tab ${range === r.key ? "vt-tab-on" : ""}`}
                onClick={() => setRange(r.key)}
              >
                {r.key}
              </button>
            ))}
          </div>
        </div>

        {tickets.isError ? (
          <div style={{ fontSize: 12, color: "var(--vt-red-dark)" }}>
            Couldn't load tickets
            {tickets.error instanceof Error ? `: ${tickets.error.message}` : "."}
          </div>
        ) : null}

        {!loading && d.attempted > 0 && d.settledCount < 3 ? (
          <div
            className="vt-panel"
            style={{ padding: "var(--space-3)", fontSize: 12, opacity: 0.75, gap: 4 }}
          >
            <strong style={{ fontFamily: "var(--font-heading)", fontWeight: 500 }}>
              Thin ledger.
            </strong>{" "}
            {d.attempted} ticket{d.attempted === 1 ? "" : "s"} in this window,{" "}
            {d.settledCount} settled. The distributions below describe those rows and
            nothing more — read them as shape, not as signal.
          </div>
        ) : null}

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))",
            gap: "var(--space-3)",
          }}
        >
          <Kpi
            label="Net profit"
            value={amount(d.netProfit, { sign: true })}
            sub={`${range} · ${d.settledCount} settled`}
            primary
          />
          <Kpi
            label="Capital deployed"
            value={round0(d.capitalDeployed)}
            sub={`${d.filled} filled of ${d.attempted} attempted`}
          />
          <Kpi
            label="Capital returned"
            value={round0(d.capitalReturned)}
            sub="settled legs only"
          />
          <Kpi
            label="Return on capital"
            value={pct(d.returnOnCapital, 2)}
            sub="net over settled capital"
            color={pnlColor(d.returnOnCapital)}
          />
          <Kpi
            label="Locked in"
            value={pendingValue === null ? "—" : amount(pendingValue, { sign: true })}
            sub={
              hedged.length === 0
                ? "nothing awaiting settlement"
                : `${round0(pendingContracts)} contracts · ${hedged.length} ticket${
                    hedged.length === 1 ? "" : "s"
                  }${unhedged.length > 0 ? ` · ${unhedged.length} unhedged` : ""}`
            }
            color={pnlColor(pendingValue)}
          />
          <Kpi
            label="Open exposure"
            value={round0(d.openExposure)}
            sub={`${d.openCount} ticket${d.openCount === 1 ? "" : "s"} unsettled`}
            color="var(--color-accent-700)"
          />
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(0,1.9fr) minmax(0,1fr)",
            gap: "var(--space-4)",
          }}
        >
          <div className="vt-panel">
            <div style={{ display: "flex", alignItems: "baseline", gap: "var(--space-3)" }}>
              <div className="vt-lab">Locked-in profit, cumulative</div>
              <span className="vt-mono" style={{ fontSize: 11, opacity: 0.55 }}>
                {range}
              </span>
              <span style={{ flex: 1 }} />
              <span className="vt-mono" style={{ fontSize: 13, color: "var(--vt-green)" }}>
                {accrual.total > 0 ? `${amount(accrual.total, { sign: true })} earned` : ""}
              </span>
            </div>
            <AccrualCurve rows={d.rows} />
            <div style={{ fontSize: 11, opacity: 0.5 }}>
              Contracts less capital, fixed at fill time and unable to move with
              quotes. Solid is settled, hatched is certain but not yet paid.
              Accrues by ticket <em>submission</em> time and follows the range
              above, so a ticket submitted before the window is out of frame even
              if it settled inside it.
            </div>
          </div>

          <BreakdownPanel
            label="Net profit by league"
            rows={d.byLeague}
            emptyNote="No tickets in this window."
          />
        </div>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit,minmax(280px,1fr))",
            gap: "var(--space-4)",
          }}
        >
          <div className="vt-panel">
            <div className="vt-lab">Captured edge distribution</div>
            {d.meanEdgeCents === null ? (
              <Empty>No ticket in this window recorded an expected edge.</Empty>
            ) : (
              <>
                <div
                  style={{
                    display: "flex",
                    alignItems: "flex-end",
                    gap: "var(--space-3)",
                    height: 130,
                  }}
                >
                  {d.edgeBuckets.map((b) => (
                    <div
                      key={b.label}
                      style={{
                        flex: 1,
                        display: "flex",
                        flexDirection: "column",
                        alignItems: "center",
                        gap: 5,
                        height: "100%",
                        justifyContent: "flex-end",
                      }}
                    >
                      <span className="vt-mono" style={{ fontSize: 11, opacity: 0.7 }}>
                        {b.count}
                      </span>
                      <div
                        style={{
                          width: "100%",
                          height: `${Math.max(3, (b.count / maxBucket) * 100)}%`,
                          background: "var(--color-accent-200)",
                          border: "1px solid var(--color-accent)",
                        }}
                      />
                      <span className="vt-mono" style={{ fontSize: 10, opacity: 0.55 }}>
                        {b.label}
                      </span>
                    </div>
                  ))}
                </div>
                <div style={{ fontSize: 11, opacity: 0.55 }}>
                  Mean expected edge {cents(d.meanEdgeCents)} per contract pair. Buckets are
                  sub-cent because fees peak at 1.75¢ (Kalshi) and 1.5¢ (Polymarket US) at
                  even money.
                </div>
              </>
            )}
          </div>

          <div className="vt-panel">
            <div className="vt-lab">Capital by venue leg</div>
            {d.byVenue.length === 0 ? (
              <Empty>No filled legs in this window.</Empty>
            ) : (
              <table className="table" style={{ fontSize: 12 }}>
                <thead>
                  <tr>
                    <th>Venue</th>
                    <th style={{ textAlign: "right" }}>Deployed</th>
                    <th style={{ textAlign: "right" }}>Returned</th>
                    <th style={{ textAlign: "right" }}>Net</th>
                    <th style={{ textAlign: "right" }}>Open</th>
                  </tr>
                </thead>
                <tbody>
                  {d.byVenue.map((v) => (
                    <tr key={v.name}>
                      <td style={{ textTransform: "capitalize" }}>{v.name}</td>
                      <td className="vt-mono" style={{ textAlign: "right" }}>
                        {round0(v.deployed)}
                      </td>
                      <td className="vt-mono" style={{ textAlign: "right" }}>
                        {round0(v.returned)}
                      </td>
                      <td
                        className="vt-mono"
                        style={{ textAlign: "right", color: pnlColor(v.net) }}
                      >
                        {amount(v.net, { sign: true })}
                      </td>
                      <td className="vt-mono" style={{ textAlign: "right", opacity: 0.6 }}>
                        {round0(v.open)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <div style={{ fontSize: 10, opacity: 0.5 }}>
              Net is measured against the settled legs alone, so an open leg is not
              booked as a loss. Deployed = settled cost + open.
            </div>

            <div className="vt-lab" style={{ marginTop: "var(--space-2)" }}>
              Execution quality
            </div>
            <div
              style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 12 }}
            >
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ opacity: 0.7 }}>Fill rate</span>
                <span className="vt-mono">
                  {d.attempted === 0 ? "—" : pct((d.filled / d.attempted) * 100, 0)}
                </span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ opacity: 0.7 }}>Both legs filled</span>
                <span className="vt-mono">{pct(d.bothLegsFilled, 0)}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ opacity: 0.7 }}>Median slippage</span>
                <span className="vt-mono">{cents(d.medianSlippageCents)}</span>
              </div>
            </div>
          </div>

          <div className="vt-panel">
            <div className="vt-lab">Outcome mix</div>
            {d.rows.length === 0 ? (
              <Empty>No tickets in this window.</Empty>
            ) : (
              <div
                style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}
              >
                {d.outcomeMix.map((o) => (
                  <div key={o.name} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                      <span>{o.name}</span>
                      <span className="vt-mono" style={{ opacity: 0.75 }}>
                        {o.count} · {o.share.toFixed(0)}%
                      </span>
                    </div>
                    <Bar value={o.count} max={d.rows.length} color={o.color} />
                  </div>
                ))}
              </div>
            )}
            <div style={{ fontSize: 11, opacity: 0.55, marginTop: "auto" }}>
              “Never filled” is a rejected or missed ticket — it never traded, so it has
              no settlement outcome. It is not a loss, and it is the number that says
              whether latency work would pay.
            </div>
          </div>
        </div>

        <div className="vt-panel">
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "var(--space-3)",
              flexWrap: "wrap",
            }}
          >
            <div className="vt-lab">Trade ledger</div>
            <span style={{ flex: 1 }} />
            <div style={{ display: "flex", gap: 2 }}>
              {FILTERS.map((f) => (
                <button
                  key={f.key}
                  type="button"
                  className={`vt-tab ${filter === f.key ? "vt-tab-on" : ""}`}
                  onClick={() => setFilter(f.key)}
                >
                  {f.key}
                </button>
              ))}
            </div>
          </div>
          <div style={{ overflowX: "auto" }} className="vt-scroll">
            <table className="table" style={{ fontSize: 12, width: "100%" }}>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Event</th>
                  <th>League</th>
                  <th>Market</th>
                  <th>Pair</th>
                  <th style={{ textAlign: "right" }}>Qty</th>
                  <th style={{ textAlign: "right" }}>Cost</th>
                  <th style={{ textAlign: "right" }}>Capital</th>
                  <th style={{ textAlign: "right" }}>Returned</th>
                  <th style={{ textAlign: "right" }}>Net</th>
                  <th style={{ textAlign: "right" }}>ROI</th>
                  <th>Status</th>
                  <th>Outcome</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr>
                    <td colSpan={13} style={{ opacity: 0.5 }}>
                      Loading…
                    </td>
                  </tr>
                ) : shown.length === 0 ? (
                  <tr>
                    <td colSpan={13} style={{ opacity: 0.5 }}>
                      No tickets match this filter in the {range} window.
                    </td>
                  </tr>
                ) : null}
                {shown.map((r) => (
                  <tr key={r.id}>
                    <td className="vt-mono" style={{ opacity: 0.7 }}>
                      {day(r.submittedAt)}
                    </td>
                    <td title={r.eventGroupId}>{r.title}</td>
                    <td>
                      <span className="tag tag-neutral" style={{ fontSize: 10 }}>
                        {r.sport.toUpperCase()}
                      </span>
                    </td>
                    <td style={{ opacity: 0.7 }}>{r.marketType}</td>
                    <td className="vt-mono" style={{ opacity: 0.7 }}>
                      {r.pair}
                    </td>
                    <td className="vt-mono" style={{ textAlign: "right" }}>
                      {r.qty === null ? "—" : r.qty.toFixed(2)}
                    </td>
                    <td className="vt-mono" style={{ textAlign: "right" }}>
                      {cents(r.costCents, 1)}
                    </td>
                    <td className="vt-mono" style={{ textAlign: "right" }}>
                      {money(r.capital)}
                    </td>
                    <td className="vt-mono" style={{ textAlign: "right" }}>
                      {money(r.returned)}
                    </td>
                    <td
                      className="vt-mono"
                      style={{ textAlign: "right", color: pnlColor(r.net), fontWeight: 600 }}
                    >
                      {r.net === null ? "—" : amount(r.net, { sign: true })}
                    </td>
                    <td className="vt-mono" style={{ textAlign: "right", opacity: 0.75 }}>
                      {pct(r.roi)}
                    </td>
                    <td>
                      <span
                        className="tag"
                        style={{ fontSize: 10 }}
                        title={r.rejectionReason ?? undefined}
                      >
                        {r.status}
                      </span>
                    </td>
                    <td>
                      <span className={`tag ${OUTCOME_TAG[r.outcome]}`} style={{ fontSize: 10 }}>
                        {OUTCOME_LABEL[r.outcome]}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
              {shown.length > 0 ? (
                <tfoot>
                  <tr style={{ borderTop: "2px solid var(--color-divider)" }}>
                    <td colSpan={5} className="vt-lab">
                      Totals · {shown.length} ticket{shown.length === 1 ? "" : "s"} shown
                    </td>
                    <td className="vt-mono" style={{ textAlign: "right", fontWeight: 600 }}>
                      {shownQty.toFixed(2)}
                    </td>
                    {/* Cost is cents per contract pair — a rate, not an extent,
                        so it does not sum. Left blank rather than filled with
                        a number that would read as a total. */}
                    <td />
                    <td className="vt-mono" style={{ textAlign: "right", fontWeight: 600 }}>
                      {amount(shownCapital)}
                    </td>
                    <td className="vt-mono" style={{ textAlign: "right", fontWeight: 600 }}>
                      {amount(shownReturned)}
                    </td>
                    <td
                      className="vt-mono"
                      style={{ textAlign: "right", fontWeight: 600, color: pnlColor(shownNet) }}
                    >
                      {shownNet === null ? "—" : amount(shownNet, { sign: true })}
                    </td>
                    <td className="vt-mono" style={{ textAlign: "right", fontWeight: 600 }}>
                      {pct(
                        shownNet === null || shownCapital <= 0
                          ? null
                          : (shownNet / shownCapital) * 100,
                        2,
                      )}
                    </td>
                    <td colSpan={2} />
                  </tr>
                </tfoot>
              ) : null}
            </table>
          </div>
        </div>

        <BreakdownPanel
          label="Net profit by market type"
          rows={d.byMarketType}
          emptyNote="No tickets in this window."
        />

        <NetOfCosts d={d} summary={summary.data} />

        <SettlementCalendar buckets={buckets} unhedged={unhedged} />
      </div>
    </div>
  );
}



/**
 * Cumulative locked-in profit across the window.
 *
 * Replaces the equity curve, which moved with open marks. A mark is noise on a
 * hedged book: a matched pair settles for exactly $1 whoever wins, so the
 * profit was fixed the moment both legs filled and no later quote can change
 * it. This accrues `qty - capital` by submit time and splits the total into
 * what has already paid out and what is merely certain.
 */
function AccrualCurve({ rows }: { rows: LedgerRow[] }) {
  const pts = rows
    .filter((r) => r.settlementValue !== null)
    .slice()
    .sort((a, b) => (a.submittedAt < b.submittedAt ? -1 : 1));
  if (pts.length < 2) {
    return <Empty>Not enough filled tickets in this window to plot.</Empty>;
  }
  let total = 0;
  let settled = 0;
  const series = pts.map((r) => {
    total += r.settlementValue ?? 0;
    if (r.net !== null) settled += r.settlementValue ?? 0;
    return { total, settled };
  });
  const W = 800;
  const H = 230;
  // Zero-based on purpose: this series only ever goes up, so a floor at its own
  // minimum would make a flat night look like a climb.
  const hi = Math.max(series[series.length - 1].total, 0.01);
  const x = (i: number) => (i / (series.length - 1)) * W;
  const y = (v: number) => H - (v / hi) * H;
  const area = (key: "total" | "settled") =>
    `M0,${H} ` +
    series.map((p, i) => `L${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ") +
    ` L${W},${H} Z`;
  const axis = [1, 0.75, 0.5, 0.25, 0].map((f) => hi * f);
  return (
    <>
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ width: "100%", height: 230 }}>
      <defs>
        <pattern id="pending-hatch" width="6" height="6" patternUnits="userSpaceOnUse">
          <path d="M0,6 L6,0" stroke="var(--vt-green)" strokeWidth="1" opacity="0.45" />
        </pattern>
      </defs>
      {axis.map((v, i) => (
        <line
          key={i}
          x1="0"
          x2={W}
          y1={y(v).toFixed(1)}
          y2={y(v).toFixed(1)}
          stroke="var(--color-divider)"
          strokeWidth="1"
        />
      ))}
      <path d={area("total")} fill="url(#pending-hatch)" />
      <path d={area("settled")} fill="var(--vt-green)" opacity="0.22" />
      <polyline
        points={series.map((p, i) => `${x(i).toFixed(1)},${y(p.total).toFixed(1)}`).join(" ")}
        fill="none"
        stroke="var(--vt-green)"
        strokeWidth="1.5"
      />
    </svg>
      {/* The window's real bounds. Naming the range ("30D") is not enough on
          its own: "All" says nothing about how much history there is, and a
          quiet stretch at either end is indistinguishable from a short window
          without dates on the axis. */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 11,
          opacity: 0.5,
        }}
        className="vt-mono"
      >
        <span>{day(pts[0].submittedAt)}</span>
        <span>{day(pts[pts.length - 1].submittedAt)}</span>
      </div>
    </>
  );
}

function bucketLabel(date: string | null): string {
  if (date === null) return "Date unknown";
  // Parsed as local midnight rather than through Date(iso), which would read a
  // bare YYYY-MM-DD as UTC and shift the label back a day west of Greenwich.
  const [y, m, dd] = date.split("-").map(Number);
  const d = new Date(y, m - 1, dd);
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

/**
 * What is owed and when it lands.
 *
 * The replacement for the per-leg positions table, and it has to carry that
 * table's one irreplaceable job: a legged arb -- one side filled, or the two
 * filled at different sizes -- has no guaranteed payout, so it is absent from
 * every bucket here and would be invisible if it were not called out
 * separately. That is the position type most worth seeing.
 */
function SettlementCalendar({
  buckets,
  unhedged,
}: {
  buckets: SettlementBucket[];
  unhedged: LedgerRow[];
}) {
  const max = Math.max(...buckets.map((b) => b.value), 0.01);
  const total = buckets.reduce((a, b) => a + b.value, 0);
  const contracts = buckets.reduce((a, b) => a + b.contracts, 0);
  return (
    <div className="vt-panel">
      <div style={{ display: "flex", alignItems: "baseline", gap: "var(--space-3)" }}>
        <div className="vt-lab">Settles on</div>
        <span style={{ flex: 1 }} />
        <span className="vt-mono" style={{ fontSize: 13 }}>
          {buckets.length > 0
            ? `${amount(total, { sign: true })} over ${round0(contracts)} contracts`
            : ""}
        </span>
      </div>
      {buckets.length === 0 ? (
        <Empty>Nothing awaiting settlement.</Empty>
      ) : (
        <table className="vt-table" style={{ width: "100%" }}>
          <thead>
            <tr>
              <th>Day</th>
              <th style={{ width: "40%" }} />
              <th style={{ textAlign: "right" }}>Profit</th>
              <th style={{ textAlign: "right" }}>Capital</th>
              <th style={{ textAlign: "right" }}>Tickets</th>
            </tr>
          </thead>
          <tbody>
            {buckets.map((b) => (
              <tr key={b.date ?? "unknown"}>
                <td style={{ whiteSpace: "nowrap", opacity: b.date === null ? 0.6 : 1 }}>
                  {bucketLabel(b.date)}
                </td>
                <td>
                  <Bar value={b.value} max={max} color="var(--vt-green)" />
                </td>
                <td className="vt-num" style={{ textAlign: "right", color: "var(--vt-green)" }}>
                  {amount(b.value, { sign: true })}
                </td>
                <td className="vt-num" style={{ textAlign: "right" }}>
                  {round0(b.capital)}
                </td>
                <td className="vt-num" style={{ textAlign: "right" }}>
                  {b.tickets}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {unhedged.length > 0 && (
        <div
          style={{
            marginTop: "var(--space-3)",
            padding: "var(--space-2)",
            border: "1px solid var(--color-divider)",
            fontSize: 12,
          }}
        >
          <span style={{ color: "var(--vt-red-dark)", fontWeight: 600 }}>
            {unhedged.length} unhedged ticket{unhedged.length === 1 ? "" : "s"}
          </span>{" "}
          — one leg filled, or the two filled at different sizes. These have no
          guaranteed payout, so they are excluded from every figure above.
          <ul style={{ margin: "var(--space-2) 0 0", paddingLeft: "1.1rem" }}>
            {unhedged.slice(0, 6).map((r) => (
              <li key={r.id}>{r.title}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
/**
 * What the strategy actually earned, and what it cost to earn it.
 *
 * The page's other tiles answer "how much profit"; this answers "profit net of
 * what". Fees are 62% of gross on the measured ledger, which is the single
 * dominant fact about this strategy and was previously not on screen anywhere
 * — it lived only in ad-hoc queries against the database.
 *
 * Everything here is scoped to **settled** tickets, matching `netProfit`. An
 * unsettled ticket has spent real fees against a profit that has not happened,
 * so including it would produce a drag figure reconciling against nothing.
 */
function NetOfCosts({
  d,
  summary,
}: {
  d: Dashboard;
  summary: PaperAccountSummary | undefined;
}) {
  const START = 2000; // seeded per venue by bootstrap and by every reset
  const balances = Object.entries(summary?.balances ?? {})
    // draftkings is seeded but never traded; showing it as 100% headroom
    // implies capacity that does not exist.
    .filter(([v]) => v === "kalshi" || v === "polymarket_us")
    .map(([venue, amt]) => ({ venue, cash: Number(amt), pct: (Number(amt) / START) * 100 }))
    .sort((a, b) => a.cash - b.cash);

  return (
    <div className="vt-panel">
      <div className="vt-lab">Net of all costs</div>
      <table className="table" style={{ fontSize: 12, width: "100%" }}>
        <tbody>
          <tr>
            <td style={{ opacity: 0.75 }}>Gross profit on settled tickets</td>
            <td className="vt-mono" style={{ textAlign: "right" }}>{amount(d.grossProfit, { sign: true })}</td>
          </tr>
          <tr>
            <td style={{ opacity: 0.75 }}>Taker fees</td>
            <td
              className="vt-mono"
              style={{ textAlign: "right", color: d.feesPaid > 0 ? "var(--vt-red-dark)" : undefined }}
            >
              {d.feesPaid > 0 ? `-${amount(d.feesPaid)}` : amount(0)}
            </td>
          </tr>
          <tr style={{ borderTop: "2px solid var(--color-divider)" }}>
            <td className="vt-lab">Net</td>
            <td
              className="vt-mono"
              style={{ textAlign: "right", fontWeight: 600, color: pnlColor(d.netProfit) }}
            >
              {amount(d.netProfit, { sign: true })}
            </td>
          </tr>
        </tbody>
      </table>

      {d.feeDragPct === null ? null : (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11 }}>
            <span style={{ opacity: 0.7 }}>Fees as a share of gross</span>
            <span className="vt-mono" style={{ fontWeight: 600 }}>{pct(d.feeDragPct, 0)}</span>
          </div>
          <div style={{ height: 8, background: "var(--color-neutral-200)", borderRadius: 2, overflow: "hidden" }}>
            <div
              style={{
                width: `${Math.min(100, Math.max(0, d.feeDragPct)).toFixed(1)}%`,
                height: "100%",
                background: "var(--vt-red-dark)",
              }}
            />
          </div>
          <div style={{ fontSize: 10, opacity: 0.6 }}>
            Both venues charge the same shape of fee, peaking at a coin flip. Totals
            price near 50/50, which is where the drag is worst.
          </div>
        </div>
      )}

      <div className="vt-lab" style={{ marginTop: "var(--space-2)" }}>Buying power</div>
      {balances.length === 0 ? (
        <Empty>No balances reported.</Empty>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {balances.map((b) => (
            <div key={b.venue} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11 }}>
                <span style={{ textTransform: "capitalize" }}>{b.venue.replace(/_/g, " ")}</span>
                <span className="vt-mono">
                  {amount(b.cash)} <span style={{ opacity: 0.55 }}>of {amount(START)}</span>
                </span>
              </div>
              <div style={{ height: 6, background: "var(--color-neutral-200)", borderRadius: 2, overflow: "hidden" }}>
                <div
                  style={{
                    width: `${Math.min(100, Math.max(0, b.pct)).toFixed(1)}%`,
                    height: "100%",
                    // Cash is capacity, so low is the warning. A venue near
                    // zero silently throttles everything and nothing else on
                    // this page says so.
                    background: b.pct < 10 ? "var(--vt-red-dark)" : b.pct < 33 ? "var(--color-accent)" : "var(--vt-green)",
                  }}
                />
              </div>
            </div>
          ))}
          <div style={{ fontSize: 10, opacity: 0.6 }}>
            A venue near zero cannot take a leg, so the bot stops filling on that
            side however good the edge is. Reset the account to reseed.
          </div>
        </div>
      )}
    </div>
  );
}
