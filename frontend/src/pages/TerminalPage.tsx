import { useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ArbOpportunity, MonitoredGroup } from "../api/types";
import { useOpportunityStream } from "../hooks/useOpportunityStream";
import { CategoryRail } from "../components/CategoryRail";
import { OpportunityCard } from "../components/OpportunityCard";
import { AccountPanel } from "../components/AccountPanel";
import {
  buildCombos,
  categoryOf,
  compareGroups,
  isCompleted,
  isCrossVenue,
} from "../lib/combo";
import { usePriceMoves } from "../hooks/usePriceMoves";
import { useNow } from "../hooks/useNow";

const VENUES = ["Polymarket", "Kalshi"];

export function TerminalPage() {
  const qc = useQueryClient();
  const { data: monitored = [] } = useQuery<MonitoredGroup[]>({
    queryKey: ["monitored"],
    queryFn: () => api.listMonitored(),
    refetchInterval: 3_000,
  });

  // GET /opportunities is the authoritative live set — entries exist only
  // while the detector still finds that edge at current quotes. The websocket
  // announces new edges but cannot announce dead ones, so it triggers a
  // refetch rather than contributing entries of its own.
  const { data: opportunities = [] } = useQuery<ArbOpportunity[]>({
    queryKey: ["opps"],
    queryFn: () => api.listOpportunities(200),
    refetchInterval: 3_000,
  });
  const lastPush = useRef(0);
  useOpportunityStream(() => {
    // Coalesce bursts; the 3s poll is the floor either way.
    const now = Date.now();
    if (now - lastPush.current < 500) return;
    lastPush.current = now;
    qc.invalidateQueries({ queryKey: ["opps"] });
  });

  const [sportFilter, setSportFilter] = useState("All");
  const [arbOnly, setArbOnly] = useState(false);
  const [filledMap, setFilledMap] = useState<Record<string, "comboA" | "comboB">>({});

  // Sorted here so every downstream list keeps a stable order. /monitored
  // returns dict-insertion order, which shifts as discovery registers games —
  // that is what made cards jump between polls.
  const liveGroups = useMemo(
    () =>
      monitored
        .filter((g) => isCrossVenue(g) && !isCompleted(g))
        .slice()
        .sort(compareGroups),
    [monitored],
  );

  const priceMoves = usePriceMoves(monitored);
  const now = useNow();

  const filtered = useMemo(() => {
    let list = liveGroups;
    if (sportFilter !== "All") {
      list = list.filter((g) => categoryOf(g).id === sportFilter);
    }
    if (arbOnly) {
      list = list.filter((g) => {
        const [a, b] = buildCombos(g);
        return a.favorable || b.favorable;
      });
    }
    return list;
  }, [liveGroups, sportFilter, arbOnly]);

  const arbCount = useMemo(
    () =>
      filtered.filter((g) => {
        const [a, b] = buildCombos(g);
        return a.favorable || b.favorable;
      }).length,
    [filtered],
  );

  return (
    <div
      style={{
        height: "100vh",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
      }}
    >
      <nav
        className="nav"
        style={{
          borderBottom: "1px solid var(--color-divider)",
          flex: "none",
        }}
      >
        <span className="nav-brand">Vantage</span>
        <span
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            fontSize: 12,
            color: "color-mix(in srgb, var(--color-text) 60%, transparent)",
          }}
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
          {filtered.length} markets live · {arbCount} arbs
        </span>
        <span style={{ flex: 1 }} />
        <a href="/admin" className="tag tag-outline" style={{ textDecoration: "none" }}>
          Admin
        </a>
        <span className="tag tag-accent">Polymarket · connected</span>
        <span className="tag tag-accent">Kalshi · connected</span>
      </nav>

      <div
        style={{
          flex: 1,
          minHeight: 0,
          display: "grid",
          gridTemplateColumns:
            "minmax(160px, 190px) minmax(0, 1fr) minmax(280px, 320px)",
          gap: 0,
        }}
      >
        <CategoryRail
          groups={liveGroups}
          sportFilter={sportFilter}
          onSportChange={setSportFilter}
          arbOnly={arbOnly}
          onToggleArbOnly={() => setArbOnly((v) => !v)}
          venues={VENUES}
        />

        <section
          className="vt-scroll"
          style={{
            minHeight: 0,
            overflowY: "auto",
            overflowX: "hidden",
            padding: "var(--space-4)",
          }}
        >
          {filtered.length === 0 ? (
            <div style={{ opacity: 0.6, fontSize: 13 }}>
              No cross-venue matchups yet — register event groups in Admin and push
              quotes.
            </div>
          ) : (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(216px, 1fr))",
                gap: "var(--space-4)",
              }}
            >
              {filtered.map((g) => (
                <OpportunityCard
                  key={g.id}
                  group={g}
                  categoryLabel={categoryOf(g).label}
                  opportunities={opportunities}
                  filledCombo={filledMap[g.id] ?? null}
                  onFilled={(id, combo) =>
                    setFilledMap((m) => ({ ...m, [id]: combo }))
                  }
                  priceMoves={priceMoves}
                  now={now}
                />
              ))}
            </div>
          )}
        </section>

        <AccountPanel />
      </div>
    </div>
  );
}
