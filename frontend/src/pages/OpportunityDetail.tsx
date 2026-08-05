import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ArbOpportunity } from "../api/types";

interface Props {
  opportunity: ArbOpportunity;
  opportunityIndex: number;
}

export function OpportunityDetail({ opportunity, opportunityIndex }: Props) {
  const qc = useQueryClient();
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const execute = useMutation({
    mutationFn: () => api.executeArb(opportunityIndex),
    onSuccess: (orderIds) => {
      setResult(`Filled ${orderIds.length} legs: ${orderIds.join(", ")}`);
      setError(null);
      qc.invalidateQueries({ queryKey: ["paper"] });
    },
    onError: (e: Error) => {
      setError(e.message);
      setResult(null);
    },
  });

  const totalFee = opportunity.legs.reduce(
    (s, l) => s + Number(l.fee),
    0
  );

  // Settlement-source warning heuristic: any two legs on different venues.
  const venues = new Set(opportunity.legs.map((l) => l.venue_id));
  const settlementWarning = venues.size > 1;

  return (
    <div className="border border-slate-800 rounded-lg p-4 space-y-4">
      <div>
        <div className="text-xs text-slate-500 uppercase">Event group</div>
        <div className="text-white font-semibold">
          {opportunity.event_group_id}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-3 text-sm">
        <div>
          <div className="text-xs text-slate-500">Stake</div>
          <div className="text-white tabular-nums">
            ${Number(opportunity.total_stake).toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-xs text-slate-500">Profit</div>
          <div className="text-emerald-400 tabular-nums">
            ${Number(opportunity.guaranteed_profit).toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-xs text-slate-500">ROI</div>
          <div className="text-emerald-400 tabular-nums">
            {(Number(opportunity.guaranteed_profit_bps) / 100).toFixed(2)}%
          </div>
        </div>
      </div>

      <div>
        <div className="text-xs text-slate-500 uppercase mb-1">Legs</div>
        <div className="space-y-1.5">
          {opportunity.legs.map((leg, i) => (
            <div
              key={i}
              className="flex items-center justify-between text-sm border border-slate-800 rounded px-2 py-1.5"
            >
              <div>
                <span
                  className={`text-xs px-1.5 py-0.5 rounded mr-2 ${
                    leg.is_buy
                      ? "bg-emerald-900 text-emerald-300"
                      : "bg-rose-900 text-rose-300"
                  }`}
                >
                  {leg.is_buy ? "BUY" : "SELL"}
                </span>
                <span className="text-slate-400 text-xs">{leg.venue_id} · </span>
                <span className="text-white">{leg.outcome_id}</span>
              </div>
              <div className="text-right tabular-nums text-xs">
                <div className="text-white">
                  {Number(leg.qty).toFixed(2)} @ ${Number(leg.price).toFixed(3)}
                </div>
                <div className="text-slate-500">fee ${Number(leg.fee).toFixed(3)}</div>
              </div>
            </div>
          ))}
        </div>
        <div className="text-xs text-slate-500 mt-2">
          Total fees: ${totalFee.toFixed(3)}
        </div>
      </div>

      {settlementWarning && (
        <div className="text-xs text-amber-300 bg-amber-950/40 border border-amber-900 rounded px-2 py-1.5">
          ⚠ Cross-venue settlement: legs resolve on different oracles/sources.
          Verify resolution rules match before treating this as riskless.
        </div>
      )}

      <button
        onClick={() => execute.mutate()}
        disabled={execute.isPending}
        className="w-full bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white font-medium py-2 rounded"
      >
        {execute.isPending ? "Executing…" : "Paper execute"}
      </button>

      {result && (
        <div className="text-xs text-emerald-300 break-all">{result}</div>
      )}
      {error && (
        <div className="text-xs text-rose-300 break-all">{error}</div>
      )}
    </div>
  );
}
