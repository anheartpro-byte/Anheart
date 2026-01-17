"use client";

import { useState, use } from "react";
import { useQuery, useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
import { useRouter, Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Activity, Heart, AlertCircle, StopCircle } from "lucide-react";
import { formatDistanceToNow } from "date-fns";

export default function LiveSessionPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const t = useTranslations();
  const router = useRouter();

  const sessionId = id as Id<"sessions">;
  const session = useQuery(api.sessions.getSession, { sessionId });
  const user = useQuery(api.users.getCurrentUser);
  const endSession = useMutation(api.sessions.endSession);

  const [showEndDialog, setShowEndDialog] = useState(false);
  const [ending, setEnding] = useState(false);

  const isDelayed =
    user?.role === "gestionnaire" || user?.role === "technician";

  const handleEndSession = async () => {
    setEnding(true);
    try {
      await endSession({ sessionId });
      router.push(`/dashboard/sessions/${sessionId}`);
    } catch (err) {
      console.error(err);
      setEnding(false);
    }
  };

  if (session === undefined) {
    return <LiveSessionSkeleton />;
  }

  if (session === null) {
    return (
      <div className="text-center py-12">
        <p className="text-muted-foreground">Session not found</p>
        <Link
          href="/dashboard/sessions"
          className="text-primary hover:underline mt-2 inline-block"
        >
          {t("common.back")}
        </Link>
      </div>
    );
  }

  if (session.status !== "active") {
    return (
      <div className="text-center py-12 space-y-4">
        <AlertCircle className="h-12 w-12 text-muted-foreground mx-auto" />
        <p className="text-lg">Session is not active</p>
        <Link href={`/dashboard/sessions/${sessionId}`}>
          <Button>View Session Details</Button>
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">
            {session.patient.firstName} {session.patient.lastName}
          </h1>
          <p className="text-muted-foreground">
            {session.machine.name} - Started{" "}
            {formatDistanceToNow(session.startedAt, { addSuffix: true })}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {isDelayed && (
            <Badge variant="secondary" className="gap-1">
              <AlertCircle className="h-3 w-3" />
              {t("ecg.delay")}
            </Badge>
          )}
          <Badge variant="default" className="gap-1">
            <span className="h-2 w-2 bg-white rounded-full animate-pulse" />
            {t("ecg.live")}
          </Badge>
          <Button variant="destructive" onClick={() => setShowEndDialog(true)}>
            <StopCircle className="h-4 w-4 mr-2" />
            {t("sessions.end")}
          </Button>
        </div>
      </div>

      {/* ECG Channels */}
      <div className="space-y-4">
        {session.channels.map((channel) => (
          <Card key={channel}>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base flex items-center gap-2">
                  <Activity className="h-4 w-4" />
                  {channel}
                </CardTitle>
                <div className="flex items-center gap-2">
                  <Heart className="h-4 w-4 text-red-500" />
                  <span className="text-xl font-bold">--</span>
                  <span className="text-sm text-muted-foreground">
                    {t("ecg.bpm")}
                  </span>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              {/* ECG Waveform Placeholder */}
              <div className="h-40 bg-muted/50 rounded-md flex items-center justify-center border-2 border-dashed">
                <div className="text-center text-muted-foreground">
                  <Activity className="h-8 w-8 mx-auto mb-2" />
                  <p className="text-sm">ECG Waveform for {channel}</p>
                  <p className="text-xs mt-1">
                    Real-time visualization placeholder
                  </p>
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* End Session Dialog */}
      <Dialog open={showEndDialog} onOpenChange={setShowEndDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("sessions.endConfirm")}</DialogTitle>
            <DialogDescription>
              This will stop recording ECG data for patient{" "}
              {session.patient.firstName} {session.patient.lastName}.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowEndDialog(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              onClick={handleEndSession}
              disabled={ending}
            >
              {ending ? t("common.loading") : t("sessions.end")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function LiveSessionSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Skeleton className="h-10 w-10" />
          <div>
            <Skeleton className="h-8 w-48" />
            <Skeleton className="h-4 w-64 mt-2" />
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Skeleton className="h-6 w-20" />
          <Skeleton className="h-10 w-32" />
        </div>
      </div>
      <Skeleton className="h-48" />
      <Skeleton className="h-48" />
    </div>
  );
}
