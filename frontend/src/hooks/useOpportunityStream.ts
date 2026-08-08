import { useEffect, useRef, useState } from "react";

/**
 * Subscribes to /ws/opportunities and signals that the live set has changed.
 *
 * Deliberately keeps no buffer of pushed opportunities. The socket announces
 * *new* edges; it never announces that an edge died, so a client-side buffer
 * of pushes is a log of things that were once true — exactly the shape of
 * staleness the server side was just fixed for. The authoritative live set is
 * whatever GET /opportunities returns, so a push simply prompts a refetch.
 *
 * Auto-reconnects with exponential backoff. `onPush` is held in a ref so a
 * caller passing an inline closure does not tear down the socket each render.
 */
export function useOpportunityStream(onPush: () => void) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const onPushRef = useRef(onPush);
  onPushRef.current = onPush;

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
      ws.onmessage = () => {
        // Payload intentionally ignored — it is a change notification.
        onPushRef.current();
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
  }, []);

  return { connected };
}
