import { useMemo, useState } from "react";
import type { ArbOpportunity, MonitoredGroup } from "../api/types";
import type { PriceMove } from "../hooks/usePriceMoves";
import { bestPair, categoryOf, compareGroups, groupStartDate, splitTitle } from "../lib/combo";
import { OpportunityRow } from "./OpportunityRow";

type SortKey = "start" | "cat" | "matchup" | "size" | "depth" | "edge" | "profit";
type SortDir = "asc" | "desc";

interface Props {
  groups: MonitoredGroup[];
  opportunities: ArbOpportunity[];
  filledMap: Record<string, boolean>;
  onFilled: (groupId: string) => void;
  priceMoves: Map<string, PriceMove>;
  now: number;
}

const COLUMNS: { key: SortKey | null; label: string; numeric?: boolean }[] = [
  { key: null, label: "" },
  { key: "cat", label: "Cat" },
  { key: "matchup", label: "Matchup" },
  { key: null, label: "Market" },
  { key: "start", label: "Start" },
  { key: null, label: "Best pair" },
  { key: "size", label: "Size", numeric: true },
  { key: "depth", label: "Book", numeric: true },
  { key: "edge", label: "Edge", numeric: true },
  { key: "profit", label: "Net $", numeric: true },
  { key: null, label: "" },
];

function num(v: string | null): number | null {
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Nulls always sort last, whichever direction is active — otherwise an
 *  unquoted row would jump to the top the moment you sort by edge.
 *
 *  `dir` is applied only to the real numeric difference, never to the
 *  null-last sentinel: multiplying the sentinel by -1 for a descending sort
 *  would flip it to null-first, which is the one thing this must never do. */
function cmpNullable(a: number | null, b: number | null, dir: number): number {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return (a - b) * dir;
}

export function OpportunityTable({
  groups,
  opportunities,
  filledMap,
  onFilled,
  priceMoves,
  now,
}: Props) {
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({
    key: "start",
    dir: "asc",
  });

  const sorted = useMemo(() => {
    const dir = sort.dir === "asc" ? 1 : -1;
    // dir is applied inside each branch (directly for the plain localeCompare
    // cases, via cmpNullable's dir param for the nullable numeric ones) so it
    // never touches the null-last sentinel — see cmpNullable above.
    const primary = (a: MonitoredGroup, b: MonitoredGroup): number => {
      switch (sort.key) {
        case "start":
          return cmpNullable(groupStartDate(a), groupStartDate(b), dir);
        case "cat":
          return categoryOf(a).label.localeCompare(categoryOf(b).label) * dir;
        case "matchup":
          return (
            splitTitle(a.title).matchup.localeCompare(splitTitle(b.title).matchup) * dir
          );
        case "size":
          return cmpNullable(bestPair(a).size, bestPair(b).size, dir);
        case "depth":
          return cmpNullable(num(a.uncapped_qty), num(b.uncapped_qty), dir);
        case "edge":
          return cmpNullable(num(a.net_edge), num(b.net_edge), dir);
        case "profit":
          return cmpNullable(num(a.net_max_profit), num(b.net_max_profit), dir);
      }
    };
    return groups.slice().sort((a, b) => {
      const p = primary(a, b);
      // Always tiebreak on compareGroups, direction-independent. Without it
      // equal keys fall back to /monitored's dict-insertion order, which
      // shifts as discovery registers games — rows would reshuffle between
      // polls and a click could land on the wrong event.
      return p !== 0 ? p : compareGroups(a, b);
    });
  }, [groups, sort]);

  const toggle = (key: SortKey) =>
    setSort((s) =>
      s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" },
    );

  return (
    <table className="table vt-table">
      <thead>
        <tr>
          {COLUMNS.map((c, i) => (
            <th
              key={i}
              className={c.numeric ? "vt-num" : undefined}
              onClick={c.key ? () => toggle(c.key!) : undefined}
              style={c.key ? { cursor: "pointer", userSelect: "none" } : undefined}
              title={c.key ? `sort by ${c.label.toLowerCase()}` : undefined}
            >
              {c.label}
              {c.key && sort.key === c.key ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sorted.map((g) => (
          <OpportunityRow
            key={g.id}
            group={g}
            opportunities={opportunities}
            filled={filledMap[g.id] === true}
            onFilled={onFilled}
            priceMoves={priceMoves}
            now={now}
          />
        ))}
      </tbody>
    </table>
  );
}
