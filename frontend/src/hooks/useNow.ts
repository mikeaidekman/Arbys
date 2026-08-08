import { useEffect, useState } from "react";

/**
 * Current epoch ms, re-rendered on an interval.
 *
 * Countdowns have to advance on their own — the monitored poll is every few
 * seconds and would make the clock stutter. One shared ticker at the page
 * level keeps every card in step and re-renders once per tick rather than
 * once per card.
 */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}
