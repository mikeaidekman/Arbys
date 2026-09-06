import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  PaperAccountSummary,
  Performance,
  TicketPage,
} from "../api/types";
import { Logo } from "../components/Logo";
import {
  RANGES,
  emptyDashboard,
  fromPerformance,
  rangeCutoff,
  settlementBuckets,
  toLedgerRow,
  type AccrualPointView,
  type Dashboard,
  type LedgerRow,
  type Outcome,
  type SettlementBucket,
} from "../lib/performance";

const ACCOUNT = "default";

/**
 * Rows per ledger page.
 *
 * There is no "fetch the whole ledger" mode any more, and that is the fix. The
 * page used to ask for a flat 1000 tickets and render whatever came back as if
 * it were everything — at the auto-trader's ~1,500/day that was **9h38m** of
 * one morning shown under an `All` label. Figures now come from the aggregate
 * endpoint, which covers every ticket in the window; this table is for reading
 * individual rows, and 50 is what fits on a screen.
 */
const PAGE_SIZE = 50;

/**
 * Cap on the unsettled tickets fetched for the settlement calendar.
 *
 * Bounded because it is deliberately *not* windowed — a ticket is owed to you
 * or it is not, regardless of which range is selected. The population is small
 * and self-limiting (37 open against 7,407 total on 2026-09-02) because
 * everything here settles within days, but the calendar states its own total
 * so a truncation would be visible rather than silent.
 */
const OPEN_LIMIT = 500;

/** Ledger filter chip -> the settlement outcome the server filters on. */
const FILTER_OUTCOME: Record<string, string | null> = {
  All: null,
  Open: "open",
  Won: "won",
  Lost: "lost",
  "Never filled": "none",
};

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
 * Ledger pager.
 *
 * Keyset, not offset: pages are cut on `(submitted_at, id)`, so a ticket
 * written while you are reading page 3 cannot shuffle a row you have already
 * seen onto page 4. There is no page count because computing one means a
 * second `COUNT` per click for a number the footer already gives as a total.
 */
function Pager({
  page,
  hasNext,
  busy,
  onPrev,
  onNext,
}: {
  page: number;
  hasNext: boolean;
  busy: boolean;
  onPrev: () => void;
  onNext: () => void;
}) {
  if (page === 0 && !hasNext) return null;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: "var(--space-3)",
        marginTop: "var(--space-3)",
      }}
    >
      <button
        type="button"
        className="btn"
        disabled={page === 0 || busy}
        onClick={onPrev}
      >
        ← Newer
      </button>
      <button type="button" className="btn" disabled={!hasNext || busy} onClick={onNext}>
        Older →
      </button>
      <span className="vt-mono" style={{ fontSize: 11, opacity: 0.5 }}>
        page {page + 1}
        {busy ? " · loading…" : ""}
      </span>
    </div>
  );
}

/** A single horizontal magnitude bar, shared by every breakdown panel. */
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

/**
 * Why tickets did not trade, counted rather than scrolled.
 *
 * Rejections are the majority of the ledger — 5,609 of 7,407 rows on
 * 2026-09-02, of which most are the paper account being out of money — and as
 * individual rows they are noise that buries the fills. As a tally they answer
 * the one question they are good for: is the bot being stopped by the market,
 * by its own caps, or by running out of cash?
 *
 * The list is capped server-side with the tail rolled into `other`, because
 * `edge_no_longer_available` carries the event group id and `stale_leg_skew`
 * carries each leg's age — 852 of the 1,011 distinct strings occurred exactly
 * once. The counts still sum to the rejected and missed totals.
 */
function WhyNotFilled({ d }: { d: Dashboard }) {
  const notFilled = (d.byStatus.rejected ?? 0) + (d.byStatus.missed ?? 0);
  const max = Math.max(...d.rejectionReasons.map((r) => r.count), 1);
  return (
    <div className="vt-panel">
      <div className="vt-lab">Why tickets did not fill</div>
      {d.rejectionReasons.length === 0 ? (
        <Empty>Nothing was rejected in this window.</Empty>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
          {d.rejectionReasons.map((r) => (
            <div key={r.reason} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12 }}>
                <span
                  className="vt-mono"
                  style={{
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                  title={r.reason}
                >
                  {r.reason}
                </span>
                <span className="vt-mono" style={{ opacity: 0.75, paddingLeft: 8 }}>
                  {r.count.toLocaleString()}
                </span>
              </div>
              <Bar value={r.count} max={max} color="var(--color-neutral-400)" />
            </div>
          ))}
        </div>
      )}
      <div style={{ fontSize: 11, opacity: 0.55, marginTop: "auto" }}>
        {notFilled.toLocaleString()} ticket{notFilled === 1 ? "" : "s"} never traded in
        this window. `insufficient_funds` is the paper account being out of cash, not
        the market refusing the trade.
      </div>
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

// Chip order. The predicates that used to live here moved to the server (see
// FILTER_OUTCOME): filtering client-side would filter only the rows this page
// happens to hold, so "Won" would show the wins *on page 3* rather than the
// wins in the window.
const FILTERS = ["All", "Open", "Won", "Lost", "Never filled"];

export function AccountPage() {
  const [range, setRange] = useState<string>(RANGES[0].key);
  const [filter, setFilter] = useState<string>("All");
  // Cursor stack, one entry per page visited. Index 0 is null — the first
  // page. Keyset paging can only step forward, so "back" means returning to a
  // cursor already held rather than computing one.
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState(0);

  // Changing the window or the filter changes the population, so every cursor
  // held for the old one now points into a different result set. Reset with
  // the selection rather than in an effect, so there is never a render where
  // a stale cursor is paired with a fresh filter.
  const resetPaging = () => {
    setCursors([null]);
    setPage(0);
  };
  const chooseRange = (key: string) => {
    setRange(key);
    resetPaging();
  };
  const chooseFilter = (key: string) => {
    setFilter(key);
    resetPaging();
  };

  const rangeOption = RANGES.find((r) => r.key === range) ?? RANGES[0];
  // The client owns the cutoff because "Today" means the *viewer's* local
  // midnight, which the server cannot know. Everything downstream of here is
  // scoped by this one instant.
  const cutoff = rangeCutoff(rangeOption);
  const since = cutoff === null ? null : new Date(cutoff).toISOString();
  const outcome = FILTER_OUTCOME[filter] ?? null;
  const cursor = cursors[page] ?? null;

  const perf = useQuery<Performance>({
    queryKey: ["paper", "performance", ACCOUNT, since],
    queryFn: () => api.paperPerformance(ACCOUNT, since),
    // Slower than the ledger on purpose. This reads every ticket in the window
    // — 0.5–0.9s over the full 7.4k-row ledger locally, and it grows with the
    // table — while a window summary barely moves between polls. The Fly notes
    // are the reason to care: sub-second quote freshness is the safety
    // argument for this system, and a reporting query must not compete with
    // it. See `loop_lag` in /health after changing this.
    refetchInterval: 60_000,
  });
  const ledger = useQuery<TicketPage>({
    queryKey: ["paper", "ledger", ACCOUNT, since, outcome, cursor],
    queryFn: () =>
      api.paperTickets(ACCOUNT, { since, outcome, cursor, limit: PAGE_SIZE }),
    // Hold the previous page while the next one loads. Without it the table
    // empties on every click, which reads as "no rows" rather than "loading".
    placeholderData: keepPreviousData,
    refetchInterval: 15_000,
  });
  // Unsettled tickets, deliberately NOT windowed by `range` — a ticket is
  // awaiting settlement now or it is not, and one submitted before the window
  // opened is still owed to you. `outcome=open` is exactly the old
  // `status === "filled" && net === null`, evaluated server-side.
  const openTickets = useQuery<TicketPage>({
    queryKey: ["paper", "open", ACCOUNT],
    queryFn: () => api.paperTickets(ACCOUNT, { outcome: "open", limit: OPEN_LIMIT }),
    refetchInterval: 30_000,
  });
  // Balances only — the rest of this page is computed from the ticket ledger.
  // Cash headroom cannot be: it is what is LEFT, not what was spent.
  const summary = useQuery<PaperAccountSummary>({
    queryKey: ["paper", "summary", ACCOUNT, "performance"],
    queryFn: () => api.paperSummary(ACCOUNT),
    refetchInterval: 15_000,
  });

  const d = perf.data ? fromPerformance(perf.data) : emptyDashboard();

  // One page of rows. Filtering is server-side now: doing it here would filter
  // only what this page happens to hold, which is the same class of quiet lie
  // the row cap was.
  const shown = (ledger.data?.items ?? []).map(toLedgerRow);
  const ledgerTotal = ledger.data?.total ?? 0;
  const hasNext = (ledger.data?.next_cursor ?? null) !== null;
  const firstRow = ledgerTotal === 0 ? 0 : page * PAGE_SIZE + 1;
  const lastRow = page * PAGE_SIZE + shown.length;

  // Page-scoped, and labelled as such in the footer. The window's real totals
  // are the KPI tiles above, which come from the aggregate over every ticket.
  // Null qty means the ticket has no legs — a miss or a pre-execution
  // rejection never traded — so it contributes 0 contracts rather than making
  // the total unknown. Same reasoning as capital below.
  const shownQty = shown.reduce((a, r) => a + (r.qty ?? 0), 0);
  const shownCapital = shown.reduce((a, r) => a + (r.capital ?? 0), 0);
  const shownReturned = shown.reduce((a, r) => a + (r.returned ?? 0), 0);
  const shownNet = shown.some((r) => r.net !== null)
    ? shown.reduce((a, r) => a + (r.net ?? 0), 0)
    : null;

  // Computed from the ticket ledger rather than the positions endpoint, which
  // this page no longer queries at all. Positions are marked at mid, and a mark
  // is noise on a hedged book: the pair settles for $1 whoever wins, so the
  // profit was fixed at fill time and no quote can change it.
  const pendingRows = (openTickets.data?.items ?? []).map(toLedgerRow);
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
  // Truncation here would understate what is owed, so say so rather than
  // quietly showing a short calendar.
  const openTruncated = (openTickets.data?.total ?? 0) > pendingRows.length;

  const maxBucket = Math.max(...d.edgeBuckets.map((b) => b.count), 1);
  const loading = perf.isLoading;

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
          {perf.isFetching || ledger.isFetching ? "refreshing" : "live"}
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
                onClick={() => chooseRange(r.key)}
              >
                {r.key}
              </button>
            ))}
          </div>
        </div>

        {perf.isError || ledger.isError ? (
          <div style={{ fontSize: 12, color: "var(--vt-red-dark)" }}>
            Couldn't load tickets
            {(perf.error ?? ledger.error) instanceof Error
              ? `: ${((perf.error ?? ledger.error) as Error).message}`
              : "."}
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
                {d.accrualTotal > 0 ? `${amount(d.accrualTotal, { sign: true })} earned` : ""}
              </span>
            </div>
            <AccrualCurve points={d.accrualCurve} />
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
            {d.attempted === 0 ? (
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
                    <Bar value={o.count} max={d.attempted} color={o.color} />
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

          <WhyNotFilled d={d} />
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
            {/* The count the page could never state before. `total` is
                counted server-side over the same filters as the rows, so this
                is the whole matching population, not the page. */}
            <span className="vt-mono" style={{ fontSize: 11, opacity: 0.6 }}>
              {ledgerTotal === 0
                ? "no tickets"
                : `${firstRow.toLocaleString()}–${lastRow.toLocaleString()} of ${ledgerTotal.toLocaleString()}`}
            </span>
            <span style={{ flex: 1 }} />
            <div style={{ display: "flex", gap: 2 }}>
              {FILTERS.map((f) => (
                <button
                  key={f}
                  type="button"
                  className={`vt-tab ${filter === f ? "vt-tab-on" : ""}`}
                  onClick={() => chooseFilter(f)}
                >
                  {f}
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
                    {/* Scoped to this page, and said so. The window's real
                        totals are the KPI tiles, which come from the
                        aggregate over every ticket — summing a page and
                        labelling it "Totals" is what made the old figures
                        wrong without looking wrong. */}
                    <td colSpan={5} className="vt-lab">
                      This page · {shown.length} of {ledgerTotal.toLocaleString()}{" "}
                      ticket{ledgerTotal === 1 ? "" : "s"}
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
          <Pager
            page={page}
            hasNext={hasNext}
            busy={ledger.isFetching}
            onPrev={() => setPage((p) => Math.max(0, p - 1))}
            onNext={() => {
              const next = ledger.data?.next_cursor ?? null;
              if (next === null) return;
              // Push the cursor only the first time this boundary is crossed;
              // stepping forward again after going back must reuse the cursor
              // already held, or the stack and the page index drift apart.
              setCursors((c) => (c.length > page + 1 ? c : [...c, next]));
              setPage((p) => p + 1);
            }}
          />
        </div>

        <BreakdownPanel
          label="Net profit by market type"
          rows={d.byMarketType}
          emptyNote="No tickets in this window."
        />

        <NetOfCosts d={d} summary={summary.data} />

        <SettlementCalendar buckets={buckets} unhedged={unhedged} truncated={openTruncated} />
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
function AccrualCurve({ points }: { points: AccrualPointView[] }) {
  // Already cumulative, already sorted, already downsampled to at most 400
  // points — the accumulation moved to the server with the rest of the
  // aggregation, so this draws the series rather than deriving it. That is
  // what lets the chart cover the whole window instead of one page of rows.
  const series = points;
  if (series.length < 2) {
    return <Empty>Not enough filled tickets in this window to plot.</Empty>;
  }
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
        <span>{day(series[0].ts)}</span>
        <span>{day(series[series.length - 1].ts)}</span>
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
  truncated,
}: {
  buckets: SettlementBucket[];
  unhedged: LedgerRow[];
  /** True when more tickets are awaiting settlement than were fetched. Said
   *  out loud rather than swallowed: this panel is a claim about what is owed,
   *  and a quietly short one understates it. */
  truncated: boolean;
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
        {truncated ? (
          <span className="tag tag-outline" style={{ fontSize: 10 }}>
            partial
          </span>
        ) : null}
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
  // Seeded per venue by bootstrap and by every reset: DEFAULT_STARTING_BALANCE
  // in arbys/backend/state.py. The two must move together.
  const START = 4000;
  // The server reports only venues that hold a paper balance, so every entry
  // here is real buying power; the page renders what it is given.
  const balances = Object.entries(summary?.balances ?? {})
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
