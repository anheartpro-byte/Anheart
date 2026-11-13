"use client";

import { useTranslations } from "next-intl";
import type { ProgrammeProfile } from "@/convex/lib/programmes";
import { formatClock } from "@/lib/training";

const WIDTH = 600;
const HEIGHT = 200;
const PHASES = ["baseline", "warmup", "hold", "cooldown", "recovery"] as const;

/**
 * The nominal timeline of a programme (ANH-137): its five phases, hold being
 * what the total leaves (`TrainingProfile.timeline` in plan.py), with the zone
 * over warm-up and hold and the heart-rate tiers over the whole session.
 */
export function ProgrammePreview({ profile }: { profile: ProgrammeProfile }) {
  const t = useTranslations("programmes.preview");
  const tPhase = useTranslations("training.phase");
  const fixed =
    profile.baselineS + profile.warmupMaxS + profile.cooldownS + profile.recoveryS;
  const durations = [
    profile.baselineS,
    profile.warmupMaxS,
    profile.totalDurationS - fixed,
    profile.cooldownS,
    profile.recoveryS,
  ];
  const tiers = [profile.hardMaxBpm, profile.criticalBpm, profile.subjectHrMax];
  if (
    !durations.every((duration) => Number.isFinite(duration) && duration > 0) ||
    ![profile.zoneLowBpm, profile.zoneHighBpm, ...tiers].every(Number.isFinite)
  )
    return <p className="text-sm text-muted-foreground">{t("unusable")}</p>;

  const low = Math.min(profile.zoneLowBpm, ...tiers) - 20;
  const high = Math.max(profile.zoneHighBpm, ...tiers) + 10;
  const x = (seconds: number) => (seconds / profile.totalDurationS) * WIDTH;
  const y = (bpm: number) => HEIGHT - ((bpm - low) / (high - low)) * HEIGHT;
  const starts = durations.map((_, index) =>
    durations.slice(0, index).reduce((sum, duration) => sum + duration, 0),
  );
  const lines = [
    { bpm: profile.hardMaxBpm, label: t("hardMax"), className: "stroke-amber-500" },
    { bpm: profile.criticalBpm, label: t("critical"), className: "stroke-red-600" },
    { bpm: profile.subjectHrMax, label: t("subjectHrMax"), className: "stroke-muted-foreground" },
  ];

  return (
    <figure className="space-y-2">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="w-full rounded-md border bg-card"
        role="img"
        aria-label={t("label", {
          low: profile.zoneLowBpm,
          high: profile.zoneHighBpm,
          duration: formatClock(profile.totalDurationS),
        })}
      >
        {PHASES.map((phase, index) => (
          <g key={phase}>
            <rect
              x={x(starts[index])}
              width={x(durations[index])}
              height={HEIGHT}
              className={index % 2 === 0 ? "fill-muted/60" : "fill-transparent"}
            />
            <text x={x(starts[index]) + 4} y={14} className="fill-muted-foreground text-[10px]">
              {tPhase(phase)}
            </text>
          </g>
        ))}
        <rect
          x={x(starts[1])}
          width={x(durations[1] + durations[2])}
          y={y(profile.zoneHighBpm)}
          height={y(profile.zoneLowBpm) - y(profile.zoneHighBpm)}
          className="fill-emerald-500/30"
        />
        {lines.map((line) => (
          <g key={line.label}>
            <line
              x1={0}
              x2={WIDTH}
              y1={y(line.bpm)}
              y2={y(line.bpm)}
              strokeDasharray="4 4"
              className={line.className}
            />
            <text x={WIDTH - 4} y={y(line.bpm) - 3} textAnchor="end" className="fill-muted-foreground text-[10px]">
              {line.label} {line.bpm}
            </text>
          </g>
        ))}
      </svg>
      <figcaption className="text-sm text-muted-foreground">
        {PHASES.map((phase, index) => `${tPhase(phase)} ${formatClock(durations[index])}`).join(" · ")}
      </figcaption>
    </figure>
  );
}
