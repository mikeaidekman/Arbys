import { useQuery } from "@tanstack/react-query";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import { api } from "../api/client";

const ACCOUNT = "default";

export function PortfolioPage() {
  const summary = useQuery({
    queryKey: ["paper", "summary", ACCOUNT],
    queryFn: () => api.paperSummary(ACCOUNT),
    refetchInterval: 5_000,
  });
  const orders = useQuery({
    queryKey: ["paper", "orders", ACCOUNT],
    queryFn: () => api.paperOrders(ACCOUNT),
    refetchInterval: 5_000,
  });
  const pnl = useQuery({
    queryKey: ["paper", "pnl", ACCOUNT],
    queryFn: () => api.paperPnl(ACCOUNT, 500),
    refetchInterval: 15_000,
  });

  const equityData = (pnl.data ?? [])
    .slice()
    .reverse()
    .map((s) => ({
      ts: new Date(s.ts).toLocaleTimeString(),
      equity: Number(s.total_equity),
      cash: Number(s.cash),
      mtm: Number(s.mtm_positions),
    }));

  const totalBalance = Object.values(summary.data?.balances ?? {}).reduce(
    (s, v) => s + Number(v),
    0
  );
  const totalRealized = Object.values(summary.data?.realized_pnl ?? {}).reduce(
    (s, v) => s + Number(v),
    0
  );

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Stat label="Total balance" value={`$${totalBalance.toFixed(2)}`} />
        <Stat
          label="Realized PnL"
          value={`$${totalRealized.toFixed(2)}`}
          tone={totalRealized >= 0 ? "up" : "down"}
        />
        <Stat
          label="Open positions"
          value={String(Object.keys(summary.data?.positions ?? {}).length)}
        />
      </div>

      <section>
        <h3 className="text-sm font-semibold text-slate-300 mb-2">
          Equity curve
        </h3>
        <div className="border border-slate-800 rounded-lg p-2 h-64 bg-slate-950">
          {equityData.length === 0 ? (
            <div className="h-full flex items-center justify-center text-slate-500 text-sm">
              No PnL snapshots yet.
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={equityData}>
                <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                <XAxis dataKey="ts" stroke="#64748b" fontSize={10} />
                <YAxis stroke="#64748b" fontSize={10} />
                <Tooltip
                  contentStyle={{
                    background: "#0f172a",
                    border: "1px solid #334155",
                    fontSize: 12,
                  }}
                />
                <Line
                  type="monotone"
                  dataKey="equity"
                  stroke="#a855f7"
                  dot={false}
                  strokeWidth={2}
                />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </section>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <section>
          <h3 className="text-sm font-semibold text-slate-300 mb-2">
            Balances by venue
          </h3>
          <SimpleTable
            headers={["Venue", "Balance", "Realized PnL"]}
            rows={Object.entries(summary.data?.balances ?? {}).map(([v, amt]) => [
              v,
              `$${Number(amt).toFixed(2)}`,
              `$${Number(summary.data?.realized_pnl?.[v] ?? 0).toFixed(2)}`,
            ])}
          />
        </section>
        <section>
          <h3 className="text-sm font-semibold text-slate-300 mb-2">
            Open positions
          </h3>
          <SimpleTable
            headers={["Outcome", "Qty"]}
            rows={Object.entries(summary.data?.positions ?? {}).map(
              ([oid, qty]) => [oid, Number(qty).toFixed(3)]
            )}
          />
        </section>
      </div>

      <section>
        <h3 className="text-sm font-semibold text-slate-300 mb-2">Orders</h3>
        <SimpleTable
          headers={["Time", "Venue", "Outcome", "Side", "Qty", "Price", "Status"]}
          rows={(orders.data ?? []).map((o) => [
            new Date(o.submitted_at).toLocaleString(),
            o.venue_id,
            o.outcome_id,
            o.is_buy ? "BUY" : "SELL",
            Number(o.qty).toFixed(3),
            `$${Number(o.limit_price).toFixed(3)}`,
            o.status,
          ])}
        />
      </section>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "up" | "down";
}) {
  const color =
    tone === "up"
      ? "text-emerald-400"
      : tone === "down"
      ? "text-rose-400"
      : "text-white";
  return (
    <div className="border border-slate-800 rounded-lg p-4">
      <div className="text-xs uppercase text-slate-500">{label}</div>
      <div className={`text-2xl font-semibold ${color} tabular-nums`}>
        {value}
      </div>
    </div>
  );
}

function SimpleTable({
  headers,
  rows,
}: {
  headers: string[];
  rows: (string | number)[][];
}) {
  return (
    <div className="border border-slate-800 rounded-lg overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-slate-900 text-slate-400 text-xs uppercase">
          <tr>
            {headers.map((h) => (
              <th key={h} className="text-left px-3 py-2">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td
                colSpan={headers.length}
                className="px-3 py-6 text-center text-slate-500 text-xs"
              >
                No data.
              </td>
            </tr>
          )}
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-slate-800">
              {r.map((c, j) => (
                <td key={j} className="px-3 py-2 tabular-nums">
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
