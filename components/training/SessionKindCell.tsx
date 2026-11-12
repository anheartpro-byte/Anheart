"use client";

import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { readBallast, readOptionalString } from "@/lib/training";
import {
  BallastBadge,
  SessionKindBadge,
  SessionOriginBadge,
} from "./TrainingBadges";

/**
 * Kind (Auto / Manuel / Enregistrement), origin and, for a bench session
 * launched from the site, "Banc lesté" badges for a session list row. Uses
 * the row's own `kind` / `origin` / `ballast` when the list query returns
 * them; otherwise falls back to reading the session's training fields.
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
  const ballast =
    rowKind === undefined ? training?.ballast : readBallast(row);
  if (kind === undefined) return null;
  return (
    <div className="flex flex-wrap gap-1">
      <SessionKindBadge kind={kind} />
      <SessionOriginBadge origin={origin} />
      <BallastBadge ballast={ballast} />
    </div>
  );
}
