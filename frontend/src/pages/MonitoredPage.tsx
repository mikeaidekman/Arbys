import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { MonitoredGroup, MonitoredLeg } from "../api/types";

function fmtPrice(v: string | null): string {
  if (v === null) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return n.toFixed(3);
}

function fmtEdgePct(edge: string | null): string {
  if (edge === null) return "—";
  const n = Number(edge);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(2)}%`;
}

function LegCell({ leg }: { leg: MonitoredLeg }) {
  const priced = leg.ask !== null || leg.bid !== null;
  return (
    <div
      className={`px-2 py-1 rounded text-xs font-mono ${
        priced ? "bg-slate-800 text-slate-200" : "bg-slate-900 text-slate-600"
      }`}
      title={leg.outcome_id}
    >
      <div className="flex justify-between gap-2">
        <span className="text-slate-400">{leg.venue_id}</span>
        <span className={leg.is_yes_side ? "text-emerald-400" : "text-rose-400"}>
          {leg.is_yes_side ? "YES" : "NO"}
        </span>
      </div>
      <div className="flex justify-between gap-2 tabular-nums">
        <span>{fmtPrice(leg.bid)}</span>
        <span className="text-slate-500">/</span>
        <span>{fmtPrice(leg.ask)}</span>
      </div>
    </div>
  );
}

export function MonitoredPage() {
  const { data = [], isLoading, error } = useQuery({
    queryKey: ["monitored"],
    queryFn: () => api.listMonitored(),
    refetchInterval: 3_000,
  });

  const arbs = data.filter((g) => g.has_arb);
  const quotedNoArb = data.filter((g) => !g.has_arb && g.fully_quoted);
  const pending = data.filter((g) => !g.fully_quoted && !g.has_arb);

  return (
    <div className="space-y-6">
      <div className="flex items-baseline gap-4">
        <h2 className="text-lg font-semibold text-white">Monitored matchups</h2>
        <div className="text-sm text-slate-500">
          {data.length} groups · {arbs.length} arb · {pending.length} waiting for quotes
        </div>
      </div>

      {error ? (
        <div className="border border-rose-800 bg-rose-950/40 text-rose-300 rounded-lg p-4 text-sm">
          Error loading: {(error as Error).message}
        </div>
      ) : null}

      {isLoading ? <div className="text-slate-500 text-sm">Loading…</div> : null}

      <MonitoredSection title="🔥 Arbitrage available" tone="hot" groups={arbs} emptyMsg="No arbitrage right now — prices are in sync across venues." />
      <MonitoredSection title="Live — no arb" tone="normal" groups={quotedNoArb} emptyMsg="No fully-quoted groups yet." />
      <MonitoredSection title="Waiting for quotes" tone="dim" groups={pending} emptyMsg="All groups fully quoted." />
    </div>
  );
}

function MonitoredSection({
  title,
  tone,
  groups,
  emptyMsg,
}: {
  title: string;
  tone: "hot" | "normal" | "dim";
  groups: MonitoredGroup[];
  emptyMsg: string;
}) {
  const border =
    tone === "hot"
      ? "border-emerald-500/60 bg-emerald-950/20"
      : tone === "dim"
      ? "border-slate-800 opacity-70"
      : "border-slate-800";
  return (
    <section>
      <h3
        className={`text-sm font-semibold uppercase tracking-wide mb-2 ${
          tone === "hot" ? "text-emerald-400" : "text-slate-400"
        }`}
      >
        {title} <span className="text-slate-600 ml-1">({groups.length})</span>
      </h3>
      {groups.length === 0 ? (
        <div className="border border-slate-800 rounded-lg p-4 text-sm text-slate-500">
          {emptyMsg}
        </div>
      ) : (
        <div className="space-y-2">
          {groups.map((g) => (
            <div
              key={g.id}
              className={`border rounded-lg p-3 ${border}`}
            >
              <div className="flex items-center gap-3 mb-2">
                <div className="text-white font-medium">{g.title}</div>
                <div className="text-xs text-slate-500 font-mono">{g.id}</div>
                <div className="ml-auto text-right">
                  <div
                    className={`text-sm font-mono tabular-nums ${
                      g.has_arb ? "text-emerald-400 font-bold" : "text-slate-400"
                    }`}
                  >
                    edge {fmtEdgePct(g.arb_edge)}
                  </div>
                  <div className="text-xs text-slate-500">
                    cheapest YES {fmtPrice(g.best_yes_ask)}
                    {g.best_yes_venue ? ` (${g.best_yes_venue})` : ""} + NO {fmtPrice(g.best_no_ask)}
                    {g.best_no_venue ? ` (${g.best_no_venue})` : ""}
                  </div>
                </div>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                {g.legs.map((leg) => (
                  <LegCell key={`${leg.venue_id}-${leg.outcome_id}`} leg={leg} />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
