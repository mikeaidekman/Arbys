import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { EventGroupLeg } from "../api/types";
import { BlueprintCard } from "../components/BlueprintCard";

const VENUES = ["polymarket_us", "kalshi", "draftkings"] as const;
const ACCOUNT = "default";

export function AdminPage() {
  const qc = useQueryClient();
  const groups = useQuery({
    queryKey: ["event-groups"],
    queryFn: api.listEventGroups,
  });

  const [id, setId] = useState("");
  const [title, setTitle] = useState("");
  const [legs, setLegs] = useState<EventGroupLeg[]>([
    { outcome_id: "", venue_id: "polymarket_us", is_yes_side: true },
    { outcome_id: "", venue_id: "kalshi", is_yes_side: false },
  ]);

  const create = useMutation({
    mutationFn: () => api.createEventGroup({ id, title, legs }),
    onSuccess: () => {
      setId("");
      setTitle("");
      setLegs([
        { outcome_id: "", venue_id: "polymarket_us", is_yes_side: true },
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

  const reset = useMutation({
    mutationFn: () => api.paperReset(ACCOUNT),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["paper"] }),
  });

  const updateLeg = (i: number, patch: Partial<EventGroupLeg>) =>
    setLegs((prev) => prev.map((l, j) => (j === i ? { ...l, ...patch } : l)));

  return (
    <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column" }}>
      <nav
        className="nav"
        style={{ borderBottom: "1px solid var(--color-divider)", flex: "none" }}
      >
        <span className="nav-brand">Vantage</span>
        <span style={{ flex: 1 }} />
        <a href="/" className="tag tag-outline" style={{ textDecoration: "none" }}>
          ← Terminal
        </a>
      </nav>

      <main
        style={{
          maxWidth: 960,
          width: "100%",
          margin: "0 auto",
          padding: "var(--space-6) var(--space-4)",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-8)",
        }}
      >
        <section>
          <h2>Event group allowlist</h2>
          <BlueprintCard style={{ padding: "var(--space-3)", marginBottom: "var(--space-4)" }}>
            <table className="table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Title</th>
                  <th>Legs</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(groups.data ?? []).length === 0 ? (
                  <tr>
                    <td colSpan={4} style={{ textAlign: "center", opacity: 0.55 }}>
                      No event groups yet — create one below.
                    </td>
                  </tr>
                ) : null}
                {(groups.data ?? []).map((g) => (
                  <tr key={g.id}>
                    <td style={{ fontWeight: 600 }}>{g.id}</td>
                    <td>{g.title}</td>
                    <td style={{ fontSize: 11, opacity: 0.7 }}>
                      {g.legs
                        .map(
                          (l) =>
                            `${l.venue_id}:${l.outcome_id}(${l.is_yes_side ? "Y" : "N"})`,
                        )
                        .join(" · ")}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <button
                        type="button"
                        className="btn btn-ghost"
                        style={{ color: "#a1263c" }}
                        onClick={() => del.mutate(g.id)}
                      >
                        delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </BlueprintCard>

          <BlueprintCard style={{ padding: "var(--space-4)", gap: "var(--space-3)" }}>
            <h4 style={{ margin: 0 }}>Add event group</h4>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-3)" }}>
              <Field label="ID">
                <input className="input" value={id} onChange={(e) => setId(e.target.value)} placeholder="eg-1" />
              </Field>
              <Field label="Title">
                <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Will X happen?" />
              </Field>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
              {legs.map((leg, i) => (
                <div
                  key={i}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "1fr 140px 120px auto",
                    gap: "var(--space-2)",
                    alignItems: "end",
                  }}
                >
                  <Field label={`Leg ${i + 1} outcome_id`}>
                    <input
                      className="input"
                      value={leg.outcome_id}
                      onChange={(e) => updateLeg(i, { outcome_id: e.target.value })}
                    />
                  </Field>
                  <Field label="Venue">
                    <select
                      className="input"
                      value={leg.venue_id}
                      onChange={(e) => updateLeg(i, { venue_id: e.target.value })}
                    >
                      {VENUES.map((v) => (
                        <option key={v} value={v}>
                          {v}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Side">
                    <select
                      className="input"
                      value={leg.is_yes_side ? "YES" : "NO"}
                      onChange={(e) => updateLeg(i, { is_yes_side: e.target.value === "YES" })}
                    >
                      <option value="YES">YES</option>
                      <option value="NO">NO</option>
                    </select>
                  </Field>
                  {legs.length > 2 ? (
                    <button
                      type="button"
                      className="btn btn-ghost"
                      style={{ color: "#a1263c", height: 36 }}
                      onClick={() => setLegs(legs.filter((_, j) => j !== i))}
                    >
                      remove
                    </button>
                  ) : (
                    <span />
                  )}
                </div>
              ))}
              <button
                type="button"
                className="btn btn-ghost"
                style={{ alignSelf: "flex-start" }}
                onClick={() =>
                  setLegs([
                    ...legs,
                    { outcome_id: "", venue_id: "polymarket_us", is_yes_side: true },
                  ])
                }
              >
                + add leg
              </button>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => create.mutate()}
                disabled={!id || !title || create.isPending}
              >
                {create.isPending ? "Saving…" : "Create"}
              </button>
              {create.error ? (
                <span style={{ fontSize: 12, color: "#a1263c" }}>
                  {(create.error as Error).message}
                </span>
              ) : null}
            </div>
          </BlueprintCard>
        </section>

        <section>
          <h2>Push quote (dev)</h2>
          <BlueprintCard style={{ padding: "var(--space-4)", gap: "var(--space-3)" }}>
            <p style={{ margin: 0, fontSize: 12, opacity: 0.7 }}>
              Manually inject a quote to test the arb engine without a live adapter. Use the
              outcome_id from a registered event group leg.
            </p>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "2fr 1fr 1fr auto",
                gap: "var(--space-2)",
                alignItems: "end",
              }}
            >
              <Field label="outcome_id">
                <input className="input" value={quoteOutcome} onChange={(e) => setQuoteOutcome(e.target.value)} />
              </Field>
              <Field label="bid">
                <input className="input" value={quoteBid} onChange={(e) => setQuoteBid(e.target.value)} />
              </Field>
              <Field label="ask">
                <input className="input" value={quoteAsk} onChange={(e) => setQuoteAsk(e.target.value)} />
              </Field>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => pushQuote.mutate()}
                disabled={!quoteOutcome || pushQuote.isPending}
                style={{ height: 36 }}
              >
                Push
              </button>
            </div>
            {pushQuote.error ? (
              <div style={{ fontSize: 12, color: "#a1263c" }}>
                {(pushQuote.error as Error).message}
              </div>
            ) : null}
            {pushQuote.isSuccess ? (
              <div style={{ fontSize: 12, color: "var(--vt-green-dark)" }}>Quote pushed.</div>
            ) : null}
          </BlueprintCard>
        </section>

        <section>
          <h2>Paper portfolio</h2>
          <BlueprintCard style={{ padding: "var(--space-4)", gap: "var(--space-3)" }}>
            <p style={{ margin: 0, fontSize: 12, opacity: 0.7 }}>
              Reset the paper portfolio. Deletes all orders, positions, PnL snapshots and
              balances, then re-seeds starting cash. Cannot be undone.
            </p>
            <div>
              <button
                type="button"
                className="btn btn-primary"
                style={{ background: "#a1263c", borderColor: "#a1263c" }}
                onClick={() => {
                  if (window.confirm("Reset the paper portfolio? This cannot be undone.")) {
                    reset.mutate();
                  }
                }}
                disabled={reset.isPending}
              >
                {reset.isPending ? "Resetting…" : "Reset portfolio"}
              </button>
              {reset.error ? (
                <span style={{ marginLeft: 12, fontSize: 12, color: "#a1263c" }}>
                  {(reset.error as Error).message}
                </span>
              ) : null}
            </div>
          </BlueprintCard>
        </section>
      </main>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
    </div>
  );
}
