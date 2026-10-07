"use client";

import { useLocale, useTranslations } from "next-intl";
import { formatDistance } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import { Badge } from "@/components/ui/badge";
import { useMachineStatusLabel } from "@/components/dashboard/statusLabels";
import { useFreshness, useFreshnessJudge } from "@/hooks/use-freshness";
import { shownMachineStatus } from "@/lib/training";

/**
 * A machine's status and last signal as every page shows them: read through
 * the freshness hook, so a machine that has stopped sending is "offline" 90 s
 * after its last signal, at the same second as its data turn "stale", and not
 * a minute or two later when the server's job writes it.
 */

/** What an answer says of a machine's signal: all three come from the server. */
export type MachineSignal = {
  status: string;
  /** When the server last heard from the machine (0: never). */
  lastHeartbeat: number;
  /** The server's clock in the answer that carries the two fields above. */
  serverNow: number;
};

const STATUS_VARIANTS = new Map<
  string,
  "default" | "secondary" | "destructive"
>([
  ["online", "default"],
  ["offline", "destructive"],
  ["in_session", "secondary"],
]);

/** The status to show for a machine, judged again every second. */
export function useShownMachineStatus(machine: MachineSignal): string {
  const { fresh } = useFreshness(machine.lastHeartbeat, machine.serverNow);
  return shownMachineStatus(machine.status, fresh);
}

export function MachineStatusBadge({
  machine,
  isDeleted,
  className,
}: {
  machine: MachineSignal;
  isDeleted?: boolean;
  className?: string;
}) {
  return (
    <ShownStatusBadge
      status={useShownMachineStatus(machine)}
      isDeleted={isDeleted}
      className={className}
    />
  );
}

/**
 * The badge of a status already worked out with `shownMachineStatus`, for a
 * component that shows other things on the same verdict. Never to be given
 * the status of a machine's record.
 */
export function ShownStatusBadge({
  status,
  isDeleted,
  className,
}: {
  status: string;
  isDeleted?: boolean;
  className?: string;
}) {
  const t = useTranslations("machines");
  const statusLabel = useMachineStatusLabel();

  if (isDeleted) {
    return (
      <Badge variant="destructive" className={className}>
        {t("deleted")}
      </Badge>
    );
  }
  return (
    <Badge
      variant={STATUS_VARIANTS.get(status) ?? "outline"}
      className={className}
    >
      {statusLabel(status)}
    </Badge>
  );
}

/** The status as plain words (the "Status" field of a machine's page). */
export function MachineStatusText({ machine }: { machine: MachineSignal }) {
  const statusLabel = useMachineStatusLabel();
  return <>{statusLabel(useShownMachineStatus(machine))}</>;
}

/**
 * "8 seconds ago" for a date the server wrote, counted again every second on
 * the server's clock: it keeps growing while the machine is silent.
 */
function useTimeAgo(at: number, serverNow: number): string {
  const locale = useLocale();
  const { now } = useFreshness(at, serverNow);
  return formatDistance(at, now, {
    addSuffix: true,
    locale: locale === "fr" ? fr : enUS,
  });
}

function TimeAgo({ at, serverNow }: { at: number; serverNow: number }) {
  return <>{useTimeAgo(at, serverNow)}</>;
}

/**
 * How long ago the server last heard from the machine, or a dash for a
 * machine that never connected.
 */
export function LastSignal({
  machine,
}: {
  machine: Pick<MachineSignal, "lastHeartbeat" | "serverNow">;
}) {
  if (machine.lastHeartbeat <= 0) return <>-</>;
  return <TimeAgo at={machine.lastHeartbeat} serverNow={machine.serverNow} />;
}

/** "Seen 8 seconds ago", under the versions a machine announced. */
export function VersionsSeen({
  at,
  serverNow,
}: {
  at: number;
  serverNow: number;
}) {
  const t = useTranslations("machines");
  return <>{t("versionSeen", { when: useTimeAgo(at, serverNow) })}</>;
}

/** How many machines are shown "online" right now. */
export function OnlineMachinesCount({
  machines,
}: {
  machines: ReadonlyArray<MachineSignal>;
}) {
  const judge = useFreshnessJudge();
  const online = machines.filter(
    (m) =>
      shownMachineStatus(m.status, judge(m.lastHeartbeat, m.serverNow).fresh) ===
      "online",
  );
  return <>{online.length}</>;
}
