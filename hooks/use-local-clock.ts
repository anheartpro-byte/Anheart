import { useEffect, useState } from "react";
import { readLocalClock, type LocalClock } from "@/lib/server-clock";

/**
 * This computer's two clocks, read again every `intervalMs`: what makes a page
 * render on time passing, not only on a change of data. The readings are only
 * ever subtracted from one another (see lib/server-clock.ts).
 */
export function useLocalClock(intervalMs = 1000): LocalClock {
  const [clock, setClock] = useState(readLocalClock);
  useEffect(() => {
    const id = setInterval(() => setClock(readLocalClock()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return clock;
}
