/**
 * ANH-147: validation evidence, configuration locks and registry display rules.
 * Decisions fail closed when the stored level disagrees with its history.
 */
import type { Doc, Id } from "../_generated/dataModel";
import type { QueryCtx } from "../_generated/server";
import { refusal, type DashboardErrorCode } from "./errors";
import { MEDICAL_PUBLIC_KEY_PEM } from "./medicalParameters";
import {
  MACHINE_VALIDATION_LEVELS,
  releaseAllowedOnMachine,
  type MachineValidationLevel,
  type ValidationLevel,
} from "./releaseValidation";

const TEST_MEDICAL_PUBLIC_KEY =
  "-----BEGIN PUBLIC KEY-----\n" +
  "MCowBQYDK2VwAyEA9lp8wwVIXK2xXJ6ljOYle/mDb4D3Y8QkSXTXi6V1kX0=\n" +
  "-----END PUBLIC KEY-----\n";
/** Exact key comparison keeps occupied validation blocked until ANH-98 replaces it. */
export const MEDICAL_KEY_IS_TEST =
  MEDICAL_PUBLIC_KEY_PEM === TEST_MEDICAL_PUBLIC_KEY;

/** Returns the ordered rank, or -1 for an unknown stored value. */
export function validationRank(level: MachineValidationLevel): number {
  return MACHINE_VALIDATION_LEVELS.indexOf(level);
}

/** Returns the only level a new review may grant. */
export function nextValidationLevel(level: MachineValidationLevel) {
  const rank = validationRank(level);
  return rank < 0 ? null : (MACHINE_VALIDATION_LEVELS[rank + 1] ?? null);
}

/** Minimum validation required to enable each central configuration flag. */
export const CONFIGURATION_LOCKS = {
  PROGRAMS_ENABLED: "auto",
  OCCUPANCY_OCCUPIED_ENABLED: "occupied",
} as const;
/** Configuration flags governed by the registry. */
export type ConfigurationKey = keyof typeof CONFIGURATION_LOCKS;

/** Allows disabling known flags and enabling only at the required effective level. */
export function configurationLockVerdict(
  level: MachineValidationLevel,
  key: ConfigurationKey,
  value: boolean,
) {
  const requiredLevel = CONFIGURATION_LOCKS[key];
  const rank = validationRank(level);
  const allowed =
    rank >= 0 && (!value || rank >= validationRank(requiredLevel));
  return {
    allowed,
    requiredLevel,
    reachedLevel: level,
    message: allowed ? null : `machine validée jusqu'au niveau ${level}`,
  };
}

/** Refuses skipped reviews and occupied validation without production medical evidence. */
export function reviewRefusal(
  current: MachineValidationLevel,
  target: Exclude<MachineValidationLevel, "none">,
  medicalKeyIsTest: boolean,
  medicalParametersVersion: string | null,
): DashboardErrorCode | null {
  if (nextValidationLevel(current) !== target)
    return "validation_level_not_next";
  if (
    target === "occupied" &&
    (medicalKeyIsTest || medicalParametersVersion === null)
  )
    return "occupied_requires_signed_medical_parameters";
  return null;
}

/** Requires a strictly lower level and an auditable reason. */
export function downgradeRefusal(
  current: MachineValidationLevel,
  target: MachineValidationLevel,
  reason: string,
): DashboardErrorCode | null {
  if (
    validationRank(target) < 0 ||
    validationRank(target) >= validationRank(current)
  )
    return "validation_level_not_lower";
  const length = reason.trim().length;
  if (length < 3 || length > 500) return "downgrade_reason_required";
  return null;
}

/** Replays valid transitions chronologically, resolving ties by their valid sequence. */
export function levelAt(
  validation: Pick<
    Doc<"machine_registry">["validation"],
    "reviews" | "downgrades"
  >,
  at: number,
): MachineValidationLevel {
  const reviews = validation.reviews
    .filter((review) => review.at <= at)
    .sort((a, b) => a.at - b.at);
  const downgrades = validation.downgrades
    .filter((downgrade) => downgrade.at <= at)
    .sort((a, b) => a.at - b.at);
  let level: MachineValidationLevel = "none";
  let reviewIndex = 0;
  let downgradeIndex = 0;
  while (reviewIndex < reviews.length || downgradeIndex < downgrades.length) {
    const review = reviews[reviewIndex];
    const downgrade = downgrades[downgradeIndex];
    if (
      review !== undefined &&
      review.at <= (downgrade?.at ?? Infinity) &&
      review.level === nextValidationLevel(level)
    ) {
      level = review.level;
      reviewIndex += 1;
    } else if (
      downgrade !== undefined &&
      downgrade.at <= (review?.at ?? Infinity) &&
      downgrade.from === level &&
      validationRank(downgrade.to) >= 0 &&
      validationRank(downgrade.to) < validationRank(level)
    ) {
      level = downgrade.to;
      downgradeIndex += 1;
    } else {
      return "none";
    }
  }
  return level;
}

/** Uses the stored level only when its review history supports it at this instant. */
export function effectiveValidationLevel(
  validation: Doc<"machine_registry">["validation"] | undefined,
  at: number,
): MachineValidationLevel {
  return validation !== undefined &&
    validation.level === levelAt(validation, at)
    ? validation.level
    : "none";
}

/** Reports incompatible software before comparing expected and reported versions. */
export function versionStatus({
  reported,
  expected,
  reportedReleaseLevel,
  machineLevel,
}: {
  reported: string | undefined;
  expected: string | undefined;
  reportedReleaseLevel: ValidationLevel | undefined;
  machineLevel: MachineValidationLevel;
}): "unknown" | "not_validated" | "up_to_date" | "outdated" {
  if (!reported) return "unknown";
  if (!releaseAllowedOnMachine(reportedReleaseLevel, machineLevel))
    return "not_validated";
  if (!expected) return "unknown";
  return reported === expected ? "up_to_date" : "outdated";
}

/** Recognises auto sessions or the exact occupancy segment written by the Pi. */
export function personOnBoard(
  session: Pick<Doc<"sessions">, "kind" | "notes">,
): boolean {
  return (
    session.kind === "auto" ||
    (session.notes?.split("; ").includes("Occupancy: occupied") ?? false)
  );
}

/** Proposes measured radii without inventing configuration defaults. */
export function radiiProposal(
  registry: Pick<
    Doc<"machine_registry">,
    "armRadiusM" | "legTipRadiusM"
  > | null,
) {
  return {
    ARM_RADIUS_M: registry?.armRadiusM ?? null,
    LEG_TIP_RADIUS_M: registry?.legTipRadiusM ?? null,
  };
}

/** Refuses a configuration write using the effective level and the stable error code. */
export async function requireConfigurationAllowed(
  ctx: Pick<QueryCtx, "db">,
  machineId: Id<"machines">,
  key: ConfigurationKey,
  value: boolean,
): Promise<void> {
  const registry = await ctx.db
    .query("machine_registry")
    .withIndex("by_machine", (q) => q.eq("machineId", machineId))
    .unique();
  if (
    !configurationLockVerdict(
      effectiveValidationLevel(registry?.validation, Date.now()),
      key,
      value,
    ).allowed
  )
    throw refusal("machine_validation_insufficient");
}
