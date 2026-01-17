"use client";

import { useState, use } from "react";
import { useQuery, useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations, useLocale } from "next-intl";
import { useRouter, Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Copy,
  Check,
  AlertTriangle,
  RefreshCw,
  Trash2,
  Pencil,
} from "lucide-react";
import { formatDistanceToNow, format } from "date-fns";
import { fr, enUS } from "date-fns/locale";
import { MachineFormModal } from "@/components/modals/MachineFormModal";

export default function MachineDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const t = useTranslations();
  const locale = useLocale();
  const router = useRouter();

  const machineId = id as Id<"machines">;
  const machine = useQuery(api.machines.getMachine, { machineId });
  const user = useQuery(api.users.getCurrentUser);

  const [showEditModal, setShowEditModal] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [showRegenerateDialog, setShowRegenerateDialog] = useState(false);
  const [newApiKey, setNewApiKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const deleteMachine = useMutation(api.machines.deleteMachine);
  const regenerateApiKey = useMutation(api.machines.regenerateApiKey);

  const dateLocale = locale === "fr" ? fr : enUS;

  const canManage = user?.role === "admin" || user?.role === "gestionnaire";

  const handleDelete = async () => {
    try {
      await deleteMachine({ machineId });
      router.push("/dashboard/machines");
    } catch (err) {
      console.error(err);
    }
  };

  const handleRegenerate = async () => {
    try {
      const result = await regenerateApiKey({ machineId });
      setNewApiKey(result.apiKey);
      setShowRegenerateDialog(false);
    } catch (err) {
      console.error(err);
    }
  };

  const copyApiKey = async () => {
    if (newApiKey) {
      await navigator.clipboard.writeText(newApiKey);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  if (machine === undefined) {
    return <MachineDetailSkeleton />;
  }

  if (machine === null) {
    return (
      <div className="text-center py-12">
        <p className="text-muted-foreground">Machine not found</p>
        <Link
          href="/dashboard/machines"
          className="text-primary hover:underline mt-2 inline-block"
        >
          {t("common.back")}
        </Link>
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{machine.name}</h1>
          <p className="text-muted-foreground">{machine.location || "-"}</p>
        </div>
        <MachineStatusBadge status={machine.status} />
      </div>

      {/* Machine Info */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>{t("machines.config")}</CardTitle>
            {canManage && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => setShowEditModal(true)}
              >
                <Pencil className="h-4 w-4 mr-2" />
                {t("common.edit")}
              </Button>
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <p className="text-sm text-muted-foreground">
                {t("machines.status")}
              </p>
              <p className="font-medium">{machine.status}</p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">
                {t("machines.lastHeartbeat")}
              </p>
              <p className="font-medium">
                {machine.lastHeartbeat > 0
                  ? formatDistanceToNow(machine.lastHeartbeat, {
                      addSuffix: true,
                      locale: dateLocale,
                    })
                  : "-"}
              </p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">
                {t("machines.sampleRate")}
              </p>
              <p className="font-medium">{machine.config.sampleRate} Hz</p>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">
                {t("machines.channels")}
              </p>
              <div className="flex gap-1 flex-wrap">
                {machine.config.channels.map((ch) => (
                  <Badge key={ch} variant="secondary">
                    {ch}
                  </Badge>
                ))}
              </div>
            </div>
            <div>
              <p className="text-sm text-muted-foreground">
                {t("machines.batchInterval")}
              </p>
              <p className="font-medium">{machine.config.batchInterval} ms</p>
            </div>
          </div>
          <Separator />
          <div>
            <p className="text-sm text-muted-foreground">Created</p>
            <p className="font-medium">
              {format(machine.createdAt, "PPP", { locale: dateLocale })}
            </p>
          </div>
        </CardContent>
      </Card>

      {/* Danger Zone */}
      {canManage && (
        <Card className="border-destructive/50">
          <CardHeader>
            <CardTitle className="text-destructive">Danger Zone</CardTitle>
            <CardDescription>Irreversible actions</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="font-medium">{t("machines.regenerateKey")}</p>
                <p className="text-sm text-muted-foreground">
                  Generate a new API key. Old key will stop working.
                </p>
              </div>
              <Button
                variant="outline"
                onClick={() => setShowRegenerateDialog(true)}
              >
                <RefreshCw className="h-4 w-4 mr-2" />
                Regenerate
              </Button>
            </div>
            <Separator />
            <div className="flex items-center justify-between">
              <div>
                <p className="font-medium">{t("common.delete")}</p>
                <p className="text-sm text-muted-foreground">
                  Permanently delete this machine.
                </p>
              </div>
              <Button
                variant="destructive"
                onClick={() => setShowDeleteDialog(true)}
                disabled={machine.status === "in_session"}
              >
                <Trash2 className="h-4 w-4 mr-2" />
                {t("common.delete")}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Delete Confirmation Dialog */}
      <Dialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("machines.deleteConfirm")}</DialogTitle>
            <DialogDescription>
              This action cannot be undone. This will permanently delete the
              machine &quot;{machine.name}&quot;.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setShowDeleteDialog(false)}
            >
              {t("common.cancel")}
            </Button>
            <Button variant="destructive" onClick={handleDelete}>
              {t("common.delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Regenerate Key Confirmation Dialog */}
      <Dialog
        open={showRegenerateDialog}
        onOpenChange={setShowRegenerateDialog}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("machines.regenerateKey")}</DialogTitle>
            <DialogDescription>
              This will invalidate the current API key immediately. The machine
              will need to be reconfigured with the new key.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setShowRegenerateDialog(false)}
            >
              {t("common.cancel")}
            </Button>
            <Button onClick={handleRegenerate}>{t("common.confirm")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* New API Key Dialog */}
      <Dialog open={newApiKey !== null} onOpenChange={() => setNewApiKey(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t("machines.apiKey")}</DialogTitle>
            <DialogDescription className="flex items-center gap-2 text-amber-600">
              <AlertTriangle className="h-4 w-4" />
              {t("machines.apiKeyWarning")}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="bg-muted p-4 rounded-md font-mono text-sm break-all">
              {newApiKey}
            </div>
            <div className="flex gap-2">
              <Button onClick={copyApiKey} variant="outline" className="flex-1">
                {copied ? (
                  <>
                    <Check className="h-4 w-4 mr-2" />
                    {t("common.copied")}
                  </>
                ) : (
                  <>
                    <Copy className="h-4 w-4 mr-2" />
                    {t("common.copy")}
                  </>
                )}
              </Button>
              <Button onClick={() => setNewApiKey(null)} className="flex-1">
                {t("common.close")}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* Edit Machine Modal */}
      <MachineFormModal
        open={showEditModal}
        onOpenChange={setShowEditModal}
        machine={machine}
      />
    </div>
  );
}

function MachineStatusBadge({ status }: { status: string }) {
  const t = useTranslations("machines");

  const variants: Record<
    string,
    "default" | "secondary" | "destructive" | "outline"
  > = {
    online: "default",
    offline: "destructive",
    in_session: "secondary",
  };

  const labels: Record<string, string> = {
    online: t("online"),
    offline: t("offline"),
    in_session: t("inSession"),
  };

  return (
    <Badge variant={variants[status] || "outline"} className="text-sm">
      {labels[status] || status}
    </Badge>
  );
}

function MachineDetailSkeleton() {
  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div className="flex items-center gap-4">
        <Skeleton className="h-10 w-10" />
        <div className="flex-1">
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-32 mt-2" />
        </div>
        <Skeleton className="h-6 w-20" />
      </div>
      <Card>
        <CardHeader>
          <Skeleton className="h-6 w-32" />
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-20 w-full" />
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
