import { useLocale, useTranslations } from "next-intl";
import { format } from "date-fns";
import { enUS, fr } from "date-fns/locale";
import type { AuditRow } from "@/lib/auditCsv";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export function AuditTable({
  rows,
  admin,
  onTarget,
}: {
  rows: AuditRow[];
  admin: boolean;
  onTarget: (row: AuditRow) => void;
}) {
  const t = useTranslations("audit");
  const locale = useLocale();
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{t("columns.at")}</TableHead>
          {admin && <TableHead>{t("columns.organization")}</TableHead>}
          {["actor", "action", "target", "source", "details", "requestId"].map(
            (column) => (
              <TableHead key={column}>{t(`columns.${column}`)}</TableHead>
            ),
          )}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row._id}>
            <TableCell className="whitespace-nowrap">
              {format(row.at, "PPp", { locale: locale === "fr" ? fr : enUS })}
            </TableCell>
            {admin && (
              <TableCell>{row.organizationName ?? t("global")}</TableCell>
            )}
            <TableCell>
              <div>
                {row.actorLabel ??
                  (row.actorId ? row.actorId.slice(0, 12) : t("roles.system"))}
              </div>
              <Badge variant="outline">{t(`roles.${row.actorRole}`)}</Badge>
            </TableCell>
            <TableCell>{t(`actions.${row.action}`)}</TableCell>
            <TableCell>
              <div>
                {t(`targets.${row.targetType}`)} :{" "}
                {row.targetLabel ?? row.targetId.slice(0, 12)}
              </div>
              <Button variant="ghost" size="sm" onClick={() => onTarget(row)}>
                {t("filterTarget")}
              </Button>
            </TableCell>
            <TableCell>{t(`sources.${row.source}`)}</TableCell>
            <TableCell>
              <ul className="text-sm text-muted-foreground">
                {Object.entries(row.details ?? {}).map(([key, value]) => (
                  <li key={key}>
                    {t(`detailLabels.${key}`)} :{" "}
                    {Array.isArray(value)
                      ? value.length
                      : String(value).slice(0, 60)}
                  </li>
                ))}
              </ul>
            </TableCell>
            <TableCell>
              <span title={row.requestId} className="font-mono text-xs">
                {row.requestId.slice(0, 12)}
              </span>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
