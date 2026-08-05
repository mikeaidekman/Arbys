import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ArbOpportunity } from "../api/types";
import { useOpportunityStream } from "../hooks/useOpportunityStream";
import { OpportunityDetail } from "./OpportunityDetail";

function pct(bps: string): string {
  const n = Number(bps);
  if (!Number.isFinite(n)) return "—";
  return `${(n / 100).toFixed(2)}%`;
}

export function OpportunitiesPage() {
  // Seed with REST snapshot; WS keeps it live.
  const { data: initial = [] } = useQuery({
    queryKey: ["opps"],
    queryFn: () => api.listOpportunities(50),
    refetchInterval: 30_000,
  });
  const { items: streamed, connected } = useOpportunityStream(100);

  const merged = useMemo(() => {
    // Streamed opps come first (most recent), then any REST-only opps.
    const seen = new Set<string>();
    const key = (o: ArbOpportunity) =>
      `${o.event_group_id}:${o.total_stake}:${o.guaranteed_profit_bps}`;
    const out: ArbOpportunity[] = [];
    for (const o of [...streamed, ...initial]) {
      const k = key(o);
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(o);
    }
    return out;
  }, [streamed, initial]);

  const [selected, setSelected] = useState<number | null>(null);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[2fr_1fr] gap-6">
      <section>
        <div className="flex items-center gap-3 mb-3">
          <h2 className="text-lg font-semibold text-white">
            Live opportunities
          </h2>
          <span
            className={`inline-flex items-center gap-1.5 text-xs ${
              connected ? "text-emerald-400" : "text-amber-400"
            }`}
          >
            <span
              className={`w-2 h-2 rounded-full ${
                connected ? "bg-emerald-400" : "bg-amber-400"
              }`}
            />
            {connected ? "streaming" : "reconnecting…"}
          </span>
          <span className="ml-auto text-xs text-slate-500">
            {merged.length} shown
          </span>
        </div>
        <div className="border border-slate-800 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-900 text-slate-400 text-xs uppercase">
              <tr>
                <th className="text-left px-3 py-2">Event group</th>
                <th className="text-left px-3 py-2">Legs</th>
                <th className="text-right px-3 py-2">Stake</th>
                <th className="text-right px-3 py-2">Profit</th>
                <th className="text-right px-3 py-2">ROI</th>
              </tr>
            </thead>
            <tbody>
              {merged.length === 0 && (
                <tr>
                  <td
                    colSpan={5}
                    className="px-3 py-8 text-center text-slate-500"
                  >
                    No opportunities yet — push quotes matching a registered
                    event group.
                  </td>
                </tr>
              )}
              {merged.map((o, i) => (
                <tr
                  key={`${o.event_group_id}-${i}`}
                  onClick={() => setSelected(i)}
                  className={`cursor-pointer border-t border-slate-800 hover:bg-slate-900 ${
                    selected === i ? "bg-slate-900" : ""
                  }`}
                >
                  <td className="px-3 py-2 text-white font-medium">
                    {o.event_group_id}
                  </td>
                  <td className="px-3 py-2 text-slate-400">
                    {o.legs
                      .map((l) => `${l.venue_id}:${l.is_buy ? "buy" : "sell"}`)
                      .join(" + ")}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    ${Number(o.total_stake).toFixed(2)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-emerald-400">
                    ${Number(o.guaranteed_profit).toFixed(2)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-emerald-400">
                    {pct(o.guaranteed_profit_bps)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <aside>
        {selected !== null && merged[selected] ? (
          <OpportunityDetail
            opportunity={merged[selected]}
            opportunityIndex={selected}
          />
        ) : (
          <div className="border border-slate-800 rounded-lg p-6 text-sm text-slate-500">
            Select an opportunity to see the leg breakdown and execute.
          </div>
        )}
      </aside>
    </div>
  );
}
