"use client";

import { useState, use, useMemo } from "react";
import { useQuery, useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { useTranslations } from "next-intl";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Activity,
  Heart,
  AlertCircle,
  StopCircle,
  Wifi,
  Clock,
  Database,
  AlertTriangle,
  CheckCircle2,
} from "lucide-react";
import { formatDistanceToNow } from "date-fns";
import { LiveSensorDisplay } from "@/components/charts/LiveSensorDisplay";
import { type SignalQuality } from "@/lib/ecg";

/**
 * ECG Live Session Page
 *
 * Data Flow:
 * 1. BITalino sensor captures ECG signal at 1000 Hz (1000 samples/second)
 * 2. Each sample is a 10-bit ADC value (0-1023)
 * 3. RPi client batches 1000 samples and sends to Convex every second
 * 4. This page queries the last 10 seconds of data and displays it
 *
 * Chart Axes:
 * - X-axis: Time in seconds (0-5s window, scrolling)
 * - Y-axis: ADC value (0-1023) - represents voltage from ECG sensor
 *   - ~512 is the baseline (no signal)
 *   - Peaks above/below are the heart's electrical activity (P, QRS, T waves)
 */

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

  // Fetch real-time ECG data (updates every time new data arrives)
  const ecgData = useQuery(api.ecgData.getRecentEcgData, {
    sessionId,
    seconds: 10, // Get last 10 seconds of data
  });

  // Get session stats
  const stats = useQuery(api.ecgData.getSessionDataStats, { sessionId });

  const [showEndDialog, setShowEndDialog] = useState(false);
  const [ending, setEnding] = useState(false);

  const isDelayed = user?.role === "gestionnaire";

  // Data arrives ALREADY TREATED from the Raspberry Pi: filtered, in mV, at the
  // transmitted rate (~250 Hz), with heart rate + quality computed on-device.
  // The UI just reads those; it does not re-filter or re-detect.
  const sampleRate = useMemo(() => {
    let rate = session?.sampleRate ?? 250;
    for (const batch of ecgData ?? []) {
      if (batch.sampleRate) rate = batch.sampleRate;
    }
    return rate;
  }, [ecgData, session?.sampleRate]);

  // Latest on-device ECG metrics (heart rate, HRV, signal quality).
  const { signalQuality, heartRate } = useMemo(() => {
    let quality: SignalQuality = "no_signal";
    let bpm: number | null = null;
    for (const batch of ecgData ?? []) {
      const m = batch.metrics?.ECG;
      if (m) {
        quality = (m.quality as SignalQuality | undefined) ?? "good";
        bpm = m.heartRate ?? null;
      }
    }
    return { signalQuality: quality, heartRate: bpm };
  }, [ecgData]);

  // "Poor" groups the two actionable bad states (hum-dominated / noisy).
  const isPoorSignal =
    signalQuality === "mains_dominated" || signalQuality === "noisy";
  const isGoodSignal = signalQuality === "good";

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

  const hasData = ecgData && ecgData.length > 0;
  const totalBatches = stats?.totalBatches ?? 0;
  const durationSeconds = stats?.durationSeconds ?? 0;
  const totalSamples = totalBatches * sampleRate; // ~1 batch per second

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
              5s delay
            </Badge>
          )}
          <Badge variant={hasData ? "default" : "secondary"} className="gap-1">
            {hasData ? (
              <>
                <span className="h-2 w-2 bg-green-400 rounded-full animate-pulse" />
                <span>Live</span>
              </>
            ) : (
              <>
                <Wifi className="h-3 w-3" />
                <span>Connecting...</span>
              </>
            )}
          </Badge>
          <Button variant="destructive" onClick={() => setShowEndDialog(true)}>
            <StopCircle className="h-4 w-4 mr-2" />
            End Session
          </Button>
        </div>
      </div>

      {/* Signal Quality Warning */}
      {isPoorSignal && (
        <Card className="border-yellow-500 bg-yellow-50 dark:bg-yellow-950">
          <CardContent className="py-4">
            <div className="flex items-start gap-3">
              <AlertTriangle className="h-6 w-6 text-yellow-600 flex-shrink-0 mt-0.5" />
              <div>
                <h3 className="font-semibold text-yellow-800 dark:text-yellow-200">
                  {signalQuality === "mains_dominated"
                    ? "Signal Dominated by Electrical Interference - Check Electrode Contact"
                    : "Poor Signal Quality - Check Electrode Connection"}
                </h3>
                <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-1">
                  The ECG signal is mostly powerline hum / noise rather than a
                  heartbeat. This usually means:
                </p>
                <ul className="text-sm text-yellow-700 dark:text-yellow-300 mt-2 list-disc list-inside space-y-1">
                  <li>ECG electrodes are not connected to the patient</li>
                  <li>Electrodes have poor skin contact (try adding gel)</li>
                  <li>Cables are loose or disconnected</li>
                </ul>
                <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-2">
                  <strong>Tip:</strong> Connect Red→Right arm, Black→Left arm,
                  White→Right leg
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Stats Cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              {isGoodSignal ? (
                <CheckCircle2 className="h-5 w-5 text-green-500" />
              ) : isPoorSignal ? (
                <AlertTriangle className="h-5 w-5 text-yellow-500" />
              ) : (
                <Wifi className="h-5 w-5 text-muted-foreground" />
              )}
              <span className="text-lg font-bold capitalize">
                {isGoodSignal ? "Good" : isPoorSignal ? "Poor" : "---"}
              </span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">Signal Quality</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              <Heart
                className={`h-5 w-5 ${heartRate ? "text-red-500 animate-pulse" : "text-muted-foreground"}`}
              />
              <span className="text-2xl font-bold">{heartRate ?? "--"}</span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">
              Heart Rate (BPM)
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              <Clock className="h-5 w-5 text-blue-500" />
              <span className="text-2xl font-bold">
                {Math.floor(durationSeconds / 60)}:
                {String(durationSeconds % 60).padStart(2, "0")}
              </span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">Duration</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              <Database className="h-5 w-5 text-green-500" />
              <span className="text-2xl font-bold">{totalBatches}</span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">Data Batches</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 pb-3">
            <div className="flex items-center gap-2">
              <Activity className="h-5 w-5 text-purple-500" />
              <span className="text-2xl font-bold">
                {(totalSamples / 1000).toFixed(0)}K
              </span>
            </div>
            <p className="text-xs text-muted-foreground mt-1">Total Samples</p>
          </CardContent>
        </Card>
      </div>

      {/* Live Sensor Charts - Professional visualizations for each sensor type */}
      <LiveSensorDisplay
        sessionId={sessionId}
        ecgData={ecgData ?? []}
        sampleRate={sampleRate}
        channels={session.channels}
      />

      {/* No data message */}
      {!hasData && (
        <Card className="border-dashed border-2">
          <CardContent className="py-12 text-center">
            <Activity className="h-16 w-16 mx-auto text-muted-foreground mb-4 animate-pulse" />
            <h3 className="text-xl font-medium mb-2">Waiting for ECG Data</h3>
            <p className="text-muted-foreground max-w-lg mx-auto mb-4">
              Make sure the BITalino device is connected and the Raspberry Pi
              client is running. Data should appear here within a few seconds.
            </p>
            <div className="text-sm text-muted-foreground space-y-1">
              <p>Expected data format:</p>
              <p className="font-mono text-xs">
                1000 samples/second @ 10-bit resolution (0-1023)
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Data Explanation Card */}
      <Card className="bg-muted/30">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Understanding the ECG Data</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground space-y-2">
          <p>
            <strong>Y-axis (0-1023):</strong> Raw ADC values from the BITalino
            sensor. This represents the electrical signal from the heart, not
            the heart rate. Values around 512 are the baseline.
          </p>
          <p>
            <strong>X-axis (seconds):</strong> Time window showing the last 5
            seconds of data. The graph scrolls as new data arrives.
          </p>
          <p>
            <strong>Sample Rate:</strong> 1000 Hz means 1000 data points per
            second. Each batch contains ~1000 samples.
          </p>
          <p>
            <strong>Heart Rate:</strong> Estimated by detecting R-wave peaks in
            the ECG signal.
          </p>
        </CardContent>
      </Card>

      {/* End Session Dialog */}
      <Dialog open={showEndDialog} onOpenChange={setShowEndDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>End Recording Session?</DialogTitle>
            <DialogDescription>
              This will stop recording ECG data for patient{" "}
              {session.patient.firstName} {session.patient.lastName}. The
              recorded data will be saved and available for review.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowEndDialog(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleEndSession}
              disabled={ending}
            >
              {ending ? "Ending..." : "End Session"}
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
        <div>
          <Skeleton className="h-8 w-48" />
          <Skeleton className="h-4 w-64 mt-2" />
        </div>
        <div className="flex items-center gap-3">
          <Skeleton className="h-6 w-16" />
          <Skeleton className="h-10 w-32" />
        </div>
      </div>
      <div className="grid grid-cols-4 gap-4">
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
      </div>
      <Skeleton className="h-[400px]" />
    </div>
  );
}
