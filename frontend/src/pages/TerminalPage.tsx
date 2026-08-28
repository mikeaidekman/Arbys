import { useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ArbOpportunity, MonitoredGroup } from "../api/types";
import { useOpportunityStream } from "../hooks/useOpportunityStream";
import { CategoryRail } from "../components/CategoryRail";
import { OpportunityTable } from "../components/OpportunityTable";
import { AccountStrip } from "../components/AccountStrip";
import {
  buildCombos,
  categoryOf,
  compareGroups,
  isCompleted,
  isCrossVenue,
} from "../lib/combo";
import { usePriceMoves } from "../hooks/usePriceMoves";
import { useNow } from "../hooks/useNow";

const VENUES = ["Polymarket US", "Kalshi"];

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
  const [filledMap, setFilledMap] = useState<Record<string, boolean>>({});

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
          // White header band over the grey page ground. Without a background
          // the nav shows the page ground, and the tiles below then float on
          // an unbroken grey field with nothing anchoring the top of it.
          background: "var(--color-surface)",
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
        <a href="/account" className="tag tag-outline" style={{ textDecoration: "none" }}>
          Account
        </a>
        <span className="tag tag-accent">Polymarket US · connected</span>
        <span className="tag tag-accent">Kalshi · connected</span>
      </nav>

      <AccountStrip />

      <CategoryRail
        groups={liveGroups}
        sportFilter={sportFilter}
        onSportChange={setSportFilter}
        arbOnly={arbOnly}
        onToggleArbOnly={() => setArbOnly((v) => !v)}
        venues={VENUES}
      />

      {/* Single column now the rail is horizontal. The table is the widest
          thing on the screen and gets the full width back. */}
      <div
        style={{
          flex: 1,
          minHeight: 0,
          display: "flex",
          padding: "0 var(--space-4) var(--space-4)",
        }}
      >
        <section
          className="vt-scroll vt-surface"
          style={{ minHeight: 0, overflow: "auto", padding: 0, flex: 1 }}
        >
          {filtered.length === 0 ? (
            <div style={{ opacity: 0.6, fontSize: 13, padding: "var(--space-4)" }}>
              No cross-venue matchups yet — register event groups in Admin and push
              quotes.
            </div>
          ) : (
            <OpportunityTable
              groups={filtered}
              opportunities={opportunities}
              filledMap={filledMap}
              onFilled={(id) => setFilledMap((m) => ({ ...m, [id]: true }))}
              priceMoves={priceMoves}
              now={now}
            />
          )}
        </section>
      </div>
    </div>
  );
}
