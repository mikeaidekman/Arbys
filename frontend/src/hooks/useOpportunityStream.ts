import { useEffect, useRef, useState } from "react";
import type { ArbOpportunity } from "../api/types";

/**
 * Subscribes to /ws/opportunities and keeps a rolling buffer of recent
 * pushed opportunities. Auto-reconnects with exponential backoff.
 */
export function useOpportunityStream(maxBuffer = 100) {
  const [items, setItems] = useState<ArbOpportunity[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let cancelled = false;
    let backoff = 500;

    const connect = () => {
      if (cancelled) return;
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${window.location.host}/ws/opportunities`);
      wsRef.current = ws;
      ws.onopen = () => {
        setConnected(true);
        backoff = 500;
      };
      ws.onmessage = (ev) => {
        try {
          const opp = JSON.parse(ev.data) as ArbOpportunity;
          setItems((prev) => [opp, ...prev].slice(0, maxBuffer));
        } catch {
          /* ignore parse errors */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!cancelled) {
          setTimeout(connect, backoff);
          backoff = Math.min(backoff * 2, 10_000);
        }
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      cancelled = true;
      wsRef.current?.close();
    };
  }, [maxBuffer]);

  return { items, connected };
}
