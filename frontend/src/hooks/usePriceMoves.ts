import { useEffect, useRef, useState } from "react";
import type { MonitoredGroup } from "../api/types";

/** Minimum ask move, in probability units, that counts as "large". 0.02 = 2¢. */
export const PRICE_MOVE_THRESHOLD = 0.02;

/** How long a move stays highlighted before fading out. */
const FLASH_MS = 2500;

export interface PriceMove {
  dir: "up" | "down";
  delta: number;
  at: number;
}

/**
 * Track per-outcome ask movements across polls and surface the large ones.
 *
 * The monitored endpoint returns a fresh snapshot every few seconds with no
 * history, so the previous ask is remembered client-side in a ref. Only moves
 * of at least `threshold` are reported, and each entry expires after
 * FLASH_MS so the highlight is a transient signal rather than sticky state.
 */
export function usePriceMoves(
  groups: MonitoredGroup[],
  threshold: number = PRICE_MOVE_THRESHOLD,
): Map<string, PriceMove> {
  const lastAsk = useRef<Map<string, number>>(new Map());
  const [moves, setMoves] = useState<Map<string, PriceMove>>(new Map());

  useEffect(() => {
    const now = Date.now();
    const detected = new Map<string, PriceMove>();
    for (const g of groups) {
      for (const leg of g.legs) {
        if (leg.ask == null) continue;
        const ask = Number(leg.ask);
        if (!Number.isFinite(ask)) continue;
        const prev = lastAsk.current.get(leg.outcome_id);
        lastAsk.current.set(leg.outcome_id, ask);
        // First sighting is a baseline, not a move.
        if (prev == null) continue;
        const delta = ask - prev;
        if (Math.abs(delta) >= threshold) {
          detected.set(leg.outcome_id, {
            dir: delta > 0 ? "up" : "down",
            delta,
            at: now,
          });
        }
      }
    }
    if (detected.size === 0) return;
    setMoves((prev) => {
      const merged = new Map(prev);
      for (const [k, v] of detected) merged.set(k, v);
      return merged;
    });
  }, [groups, threshold]);

  useEffect(() => {
    if (moves.size === 0) return;
    const timer = setInterval(() => {
      const now = Date.now();
      setMoves((prev) => {
        let dirty = false;
        const next = new Map(prev);
        for (const [k, v] of prev) {
          if (now - v.at > FLASH_MS) {
            next.delete(k);
            dirty = true;
          }
        }
        return dirty ? next : prev;
      });
    }, 500);
    return () => clearInterval(timer);
  }, [moves.size]);

  return moves;
}
