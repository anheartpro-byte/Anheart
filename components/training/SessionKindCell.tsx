"use client";

import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { readOptionalString } from "@/lib/training";
import { SessionKindBadge, SessionOriginBadge } from "./TrainingBadges";

/**
 * Kind (Auto / Manuel / Enregistrement) and origin badges for a session list
 * row. Uses the row's own `kind` / `origin` when the list query returns them;
 * otherwise falls back to reading the session's training fields.
 */
export function SessionKindCell({ row }: { row: { _id: Id<"sessions"> } }) {
  const rowKind = readOptionalString(row, "kind");
  const rowOrigin = readOptionalString(row, "origin");
  const training = useQuery(
    api.training.getTrainingSession,
    rowKind === undefined ? { sessionId: row._id } : "skip",
  );
  const kind = rowKind ?? training?.kind;
  const origin = rowOrigin ?? training?.origin;
  if (kind === undefined) return null;
  return (
    <div className="flex flex-wrap gap-1">
      <SessionKindBadge kind={kind} />
      <SessionOriginBadge origin={origin} />
    </div>
  );
}
