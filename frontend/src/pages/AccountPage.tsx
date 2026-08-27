import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { PaperPosition, PnlSnapshot, Ticket } from "../api/types";
import {
  RANGES,
  summarize,
  type CurvePoint,
  type Dashboard,
  type LedgerRow,
  type Outcome,
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

function Corners() {
  return (
    <>
      <i className="corner tl" />
      <i className="corner tr" />
      <i className="corner bl" />
      <i className="corner br" />
    </>
  );
}

function Kpi({
  label,
  value,
  sub,
  color,
}: {
  label: string;
  value: string;
  sub: string;
  color?: string;
}) {
  return (
    <div className="vt-kpi blueprint">
      <Corners />
      <div className="vt-lab">{label}</div>
      <div
        className="vt-mono"
        style={{ fontSize: 22, fontWeight: 600, color: color ?? "var(--color-text)" }}
      >
        {value}
      </div>
      <div style={{ fontSize: 11, opacity: 0.55 }}>{sub}</div>
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
function Curve({ points }: { points: CurvePoint[] }) {
  if (points.length < 2) {
    return (
      <Empty>
        Not enough snapshots in this window — one is written every 30 seconds
        while the backend is running.
      </Empty>
    );
  }
  const W = 800;
  const H = 230;
  const values = points.map((p) => p.value);
  // Zero is always in frame: without it a series that never crosses the axis
  // reads as if it started at its own minimum.
  const lo = Math.min(0, ...values);
  const hi = Math.max(0, ...values);
  const span = hi - lo || 1;
  const y = (v: number) => H - ((v - lo) / span) * H;
  const pts = points.map((p, i) => [(i / (points.length - 1)) * W, y(p.value)]);
  const line = pts.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join(" ");
  const zeroY = y(0);
  const area = `M0,${zeroY.toFixed(1)} L${line.split(" ").join(" L")} L${W},${zeroY.toFixed(1)} Z`;
  const axis = [1, 0.75, 0.5, 0.25, 0].map((f) => lo + span * f);

  return (
    <>
      <div style={{ position: "relative", height: H }}>
        <svg
          width="100%"
          height={H}
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          style={{ display: "block", overflow: "visible" }}
          role="img"
          aria-label="Cumulative net profit and loss"
        >
          <g stroke="var(--color-divider)" strokeWidth={1} vectorEffect="non-scaling-stroke">
            {[0, 0.25, 0.5, 0.75, 1].map((f) => (
              <line key={f} x1={0} y1={f * H} x2={W} y2={f * H} />
            ))}
          </g>
          <path d={area} fill="var(--color-accent-100)" stroke="none" />
          {lo < 0 ? (
            <line
              x1={0}
              y1={zeroY}
              x2={W}
              y2={zeroY}
              stroke="var(--color-text)"
              strokeWidth={1}
              strokeDasharray="3 3"
              opacity={0.35}
              vectorEffect="non-scaling-stroke"
            />
          ) : null}
          <polyline
            points={line}
            fill="none"
            stroke="var(--vt-green)"
            strokeWidth={2}
            vectorEffect="non-scaling-stroke"
          />
        </svg>
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            flexDirection: "column",
            justifyContent: "space-between",
            pointerEvents: "none",
          }}
        >
          {axis.map((v, i) => (
            <div
              key={i}
              className="vt-mono"
              style={{
                fontSize: 10,
                opacity: 0.5,
                background: "var(--color-bg)",
                alignSelf: "flex-start",
                paddingRight: 4,
              }}
            >
              {amount(v, { sign: true })}
            </div>
          ))}
        </div>
      </div>
      <div
        className="vt-mono"
        style={{ display: "flex", justifyContent: "space-between", fontSize: 10, opacity: 0.5 }}
      >
        <span>{day(points[0].ts)}</span>
        <span>{day(points[Math.floor(points.length / 2)].ts)}</span>
        <span>{day(points[points.length - 1].ts)}</span>
      </div>
    </>
  );
}

/** Signed proportional bar. Negative nets draw left-anchored in red. */
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
    <div className="vt-panel blueprint">
      <Corners />
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
  const positions = useQuery<PaperPosition[]>({
    queryKey: ["paper", "positions", ACCOUNT],
    queryFn: () => api.paperPositions(ACCOUNT),
    refetchInterval: 10_000,
  });

  const days = RANGES.find((r) => r.key === range)?.days ?? null;
  // Snapshots arrive newest-first; the curve reads left to right.
  const snapshots = [...(pnl.data ?? [])].reverse();
  const d = summarize(tickets.data ?? [], snapshots, days);

  const shown = d.rows.filter(
    (r) => FILTERS.find((f) => f.key === filter)?.match(r) ?? true,
  );
  const shownCapital = shown.reduce((a, r) => a + (r.capital ?? 0), 0);
  const shownReturned = shown.reduce((a, r) => a + (r.returned ?? 0), 0);
  const shownNet = shown.some((r) => r.net !== null)
    ? shown.reduce((a, r) => a + (r.net ?? 0), 0)
    : null;

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
          background: "var(--color-bg)",
        }}
      >
        <span className="nav-brand">Vantage</span>
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
            color={pnlColor(d.netProfit)}
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
            label="Hit rate"
            value={pct(d.hitRate, 0)}
            sub={`${d.wonCount} of ${d.settledCount} settled`}
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
          <div className="vt-panel blueprint">
            <Corners />
            <div style={{ display: "flex", alignItems: "baseline", gap: "var(--space-3)" }}>
              <div className="vt-lab">Cumulative net P&amp;L</div>
              <span style={{ flex: 1 }} />
              <span
                className="vt-mono"
                style={{
                  fontSize: 13,
                  color: pnlColor(d.curve.length > 0 ? d.curve[d.curve.length - 1].value : null),
                }}
              >
                {d.curve.length > 0
                  ? `${amount(d.curve[d.curve.length - 1].value, { sign: true })} equity change`
                  : ""}
              </span>
            </div>
            {pnl.isError ? (
              <Empty>
                Couldn't load equity history
                {pnl.error instanceof Error ? `: ${pnl.error.message}` : "."}
              </Empty>
            ) : (
              <Curve points={d.curve} />
            )}
            <div style={{ fontSize: 11, opacity: 0.5 }}>
              Equity change from the start of the window — includes open marks, so it
              moves between settlements.
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
          <div className="vt-panel blueprint">
            <Corners />
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

          <div className="vt-panel blueprint">
            <Corners />
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

          <div className="vt-panel blueprint">
            <Corners />
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

        <div className="vt-panel blueprint">
          <Corners />
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
                    <td colSpan={7} className="vt-lab">
                      Totals · {shown.length} ticket{shown.length === 1 ? "" : "s"} shown
                    </td>
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

        <OpenPositions query={positions} />
      </div>
    </div>
  );
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

/**
 * Per-leg open positions.
 *
 * Not in the artboard, kept deliberately: the "open exposure" tile is one
 * number, and an arb that has legged — one side filled, the other not — is
 * invisible in an aggregate but obvious here.
 */
function OpenPositions({
  query,
}: {
  query: { data?: PaperPosition[]; isLoading: boolean; isError: boolean; error: unknown };
}) {
  const open = groupPositionsByEvent((query.data ?? []).filter((p) => Number(p.qty) !== 0));
  return (
    <div className="vt-panel blueprint">
      <Corners />
      <div className="vt-lab">Open positions</div>
      <div style={{ overflowX: "auto" }} className="vt-scroll">
        <table className="table" style={{ fontSize: 12, width: "100%" }}>
          <thead>
            <tr>
              <th>Event</th>
              <th>Legs</th>
              <th style={{ textAlign: "right" }}>Capital</th>
              <th style={{ textAlign: "right" }}>Mark value</th>
              <th style={{ textAlign: "right" }}>Unrealized</th>
            </tr>
          </thead>
          <tbody>
            {query.isLoading ? (
              <tr>
                <td colSpan={5} style={{ opacity: 0.5 }}>
                  Loading…
                </td>
              </tr>
            ) : query.isError ? (
              <tr>
                <td colSpan={5} style={{ color: "var(--vt-red-dark)" }}>
                  Couldn't load open positions
                  {query.error instanceof Error ? `: ${query.error.message}` : "."}
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
                      {Number(p.qty).toFixed(2)} @ {(Number(p.avg_price) * 100).toFixed(1)}¢ →
                      mark {p.mark === null ? "—" : `${(Number(p.mark) * 100).toFixed(1)}¢`}
                    </div>
                  ))}
                </td>
                <td className="vt-mono" style={{ textAlign: "right" }}>
                  {amount(row.capital)}
                </td>
                <td className="vt-mono" style={{ textAlign: "right" }}>
                  {amount(row.markValue)}
                  {row.unmarkedLegs > 0 ? (
                    <div style={{ fontSize: 10, opacity: 0.7 }}>
                      {row.unmarkedLegs} leg{row.unmarkedLegs === 1 ? "" : "s"} unquoted
                    </div>
                  ) : null}
                </td>
                <td
                  className="vt-mono"
                  style={{ textAlign: "right", color: pnlColor(row.unrealized) }}
                >
                  {amount(row.unrealized, { sign: true })}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
