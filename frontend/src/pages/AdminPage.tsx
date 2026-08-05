import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { EventGroupLeg } from "../api/types";

const VENUES = ["polymarket", "kalshi", "draftkings"] as const;

export function AdminPage() {
  const qc = useQueryClient();
  const groups = useQuery({
    queryKey: ["event-groups"],
    queryFn: api.listEventGroups,
  });

  const [id, setId] = useState("");
  const [title, setTitle] = useState("");
  const [legs, setLegs] = useState<EventGroupLeg[]>([
    { outcome_id: "", venue_id: "polymarket", is_yes_side: true },
    { outcome_id: "", venue_id: "kalshi", is_yes_side: false },
  ]);

  const create = useMutation({
    mutationFn: () => api.createEventGroup({ id, title, legs }),
    onSuccess: () => {
      setId("");
      setTitle("");
      setLegs([
        { outcome_id: "", venue_id: "polymarket", is_yes_side: true },
        { outcome_id: "", venue_id: "kalshi", is_yes_side: false },
      ]);
      qc.invalidateQueries({ queryKey: ["event-groups"] });
    },
  });

  const del = useMutation({
    mutationFn: (gid: string) => api.deleteEventGroup(gid),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["event-groups"] }),
  });

  const [quoteOutcome, setQuoteOutcome] = useState("");
  const [quoteBid, setQuoteBid] = useState("0.50");
  const [quoteAsk, setQuoteAsk] = useState("0.50");
  const pushQuote = useMutation({
    mutationFn: () => api.pushQuote(quoteOutcome, quoteBid, quoteAsk),
  });

  const updateLeg = (i: number, patch: Partial<EventGroupLeg>) =>
    setLegs((prev) => prev.map((l, j) => (j === i ? { ...l, ...patch } : l)));

  return (
    <div className="space-y-8">
      <section>
        <h2 className="text-lg font-semibold text-white mb-3">
          Event group allowlist
        </h2>
        <div className="border border-slate-800 rounded-lg overflow-hidden mb-6">
          <table className="w-full text-sm">
            <thead className="bg-slate-900 text-xs uppercase text-slate-400">
              <tr>
                <th className="text-left px-3 py-2">ID</th>
                <th className="text-left px-3 py-2">Title</th>
                <th className="text-left px-3 py-2">Legs</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {(groups.data ?? []).length === 0 && (
                <tr>
                  <td colSpan={4} className="px-3 py-6 text-center text-slate-500 text-xs">
                    No event groups yet — create one below.
                  </td>
                </tr>
              )}
              {(groups.data ?? []).map((g) => (
                <tr key={g.id} className="border-t border-slate-800">
                  <td className="px-3 py-2 text-white font-medium">{g.id}</td>
                  <td className="px-3 py-2">{g.title}</td>
                  <td className="px-3 py-2 text-xs text-slate-400">
                    {g.legs
                      .map(
                        (l) =>
                          `${l.venue_id}:${l.outcome_id}(${l.is_yes_side ? "Y" : "N"})`
                      )
                      .join(" · ")}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button
                      onClick={() => del.mutate(g.id)}
                      className="text-xs text-rose-400 hover:text-rose-300"
                    >
                      delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="border border-slate-800 rounded-lg p-4 space-y-3">
          <h3 className="text-sm font-semibold text-slate-300">
            Add event group
          </h3>
          <div className="grid grid-cols-2 gap-3">
            <Input label="ID" value={id} onChange={setId} placeholder="eg-1" />
            <Input
              label="Title"
              value={title}
              onChange={setTitle}
              placeholder="Will X happen?"
            />
          </div>
          <div className="space-y-2">
            {legs.map((leg, i) => (
              <div key={i} className="grid grid-cols-[1fr_140px_120px_auto] gap-2 items-end">
                <Input
                  label={`Leg ${i + 1} outcome_id`}
                  value={leg.outcome_id}
                  onChange={(v) => updateLeg(i, { outcome_id: v })}
                />
                <Select
                  label="Venue"
                  value={leg.venue_id}
                  onChange={(v) => updateLeg(i, { venue_id: v })}
                  options={VENUES as unknown as string[]}
                />
                <Select
                  label="Side"
                  value={leg.is_yes_side ? "YES" : "NO"}
                  onChange={(v) => updateLeg(i, { is_yes_side: v === "YES" })}
                  options={["YES", "NO"]}
                />
                {legs.length > 2 && (
                  <button
                    onClick={() => setLegs(legs.filter((_, j) => j !== i))}
                    className="text-xs text-rose-400 h-9"
                  >
                    remove
                  </button>
                )}
              </div>
            ))}
            <button
              onClick={() =>
                setLegs([
                  ...legs,
                  { outcome_id: "", venue_id: "polymarket", is_yes_side: true },
                ])
              }
              className="text-xs text-purple-400 hover:text-purple-300"
            >
              + add leg
            </button>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={() => create.mutate()}
              disabled={!id || !title || create.isPending}
              className="bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded"
            >
              {create.isPending ? "Saving…" : "Create"}
            </button>
            {create.error && (
              <span className="text-xs text-rose-400">
                {(create.error as Error).message}
              </span>
            )}
          </div>
        </div>
      </section>

      <section>
        <h2 className="text-lg font-semibold text-white mb-3">
          Push quote (dev)
        </h2>
        <div className="border border-slate-800 rounded-lg p-4 space-y-3">
          <p className="text-xs text-slate-500">
            Manually inject a quote to test the arb engine without a live
            adapter. Use the outcome_id from a registered event group leg.
          </p>
          <div className="grid grid-cols-[2fr_1fr_1fr_auto] gap-2 items-end">
            <Input label="outcome_id" value={quoteOutcome} onChange={setQuoteOutcome} />
            <Input label="bid" value={quoteBid} onChange={setQuoteBid} />
            <Input label="ask" value={quoteAsk} onChange={setQuoteAsk} />
            <button
              onClick={() => pushQuote.mutate()}
              disabled={!quoteOutcome || pushQuote.isPending}
              className="bg-slate-700 hover:bg-slate-600 disabled:opacity-50 text-white text-sm px-4 py-2 rounded h-9"
            >
              Push
            </button>
          </div>
          {pushQuote.error && (
            <div className="text-xs text-rose-400">
              {(pushQuote.error as Error).message}
            </div>
          )}
          {pushQuote.isSuccess && (
            <div className="text-xs text-emerald-400">Quote pushed.</div>
          )}
        </div>
      </section>

      <section>
        <h2 className="text-lg font-semibold text-white mb-3">
          Paper broker settings
        </h2>
        <div className="border border-slate-800 rounded-lg p-4 text-sm text-slate-500">
          Slippage bps, latency ms, per-venue starting balances — coming soon.
          Currently configured in <code className="text-slate-300">arbys/backend/state.py</code>.
        </div>
      </section>
    </div>
  );
}

function Input({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="text-xs text-slate-400 flex flex-col gap-1">
      {label}
      <input
        className="bg-slate-900 border border-slate-700 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-purple-500"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
      />
    </label>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  return (
    <label className="text-xs text-slate-400 flex flex-col gap-1">
      {label}
      <select
        className="bg-slate-900 border border-slate-700 rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-purple-500"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}
