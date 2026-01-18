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
import { LiveECGWaveform } from "@/components/ECGWaveform";

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

  // Calculate signal quality and heart rate from ECG data (derived state)
  const { signalQuality, heartRate } = useMemo(() => {
    if (!ecgData || ecgData.length === 0) {
      return { signalQuality: "no_signal" as const, heartRate: null };
    }

    // Get all ECG values from recent batches
    const allValues: number[] = [];
    for (const batch of ecgData) {
      const ecgChannel = batch.samples.find(
        (s) => s.channel.toUpperCase() === "ECG",
      );
      if (ecgChannel) {
        allValues.push(...ecgChannel.values);
      }
    }

    // At 100 Hz, we need at least 200 samples (2 seconds) for analysis
    if (allValues.length < 200) {
      return { signalQuality: "no_signal" as const, heartRate: null };
    }

    // Use last 5 seconds for heart rate calculation (500 samples at 100 Hz)
    const values = allValues.slice(-500);

    // Check signal quality first
    // Count how many values are clipping (at 0 or >= 1020)
    const clippingLow = values.filter((v) => v <= 5).length;
    const clippingHigh = values.filter((v) => v >= 1018).length;
    const clippingPercent = (clippingLow + clippingHigh) / values.length;

    // Calculate standard deviation
    const mean = values.reduce((a, b) => a + b, 0) / values.length;
    const variance =
      values.reduce((a, b) => a + Math.pow(b - mean, 2), 0) / values.length;
    const stdDev = Math.sqrt(variance);

    // Determine signal quality
    if (clippingPercent > 0.3 || stdDev < 10 || stdDev > 400) {
      return { signalQuality: "poor" as const, heartRate: null };
    }

    // Find threshold for peak detection (adaptive)
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min;

    // For a good ECG, the R-wave peak is typically the highest point
    // Use a threshold at 70% of the range above the minimum
    const threshold = min + range * 0.7;

    // Count peaks (R-waves in ECG)
    // At 100 Hz: 40 samples = 400ms (max 150 BPM)
    const minPeakDistance = 40;
    let peaks = 0;
    let lastPeakIdx = -minPeakDistance;

    for (let i = 3; i < values.length - 3; i++) {
      // Peak detection: local maximum above threshold
      if (
        values[i] > threshold &&
        values[i] >= values[i - 1] &&
        values[i] >= values[i + 1] &&
        values[i] >= Math.max(...values.slice(Math.max(0, i - 3), i)) &&
        values[i] >=
          Math.max(...values.slice(i + 1, Math.min(values.length, i + 4))) &&
        i - lastPeakIdx > minPeakDistance
      ) {
        peaks++;
        lastPeakIdx = i;
      }
    }

    // Convert to BPM: peaks in 5 seconds * 12 = BPM
    if (peaks >= 2) {
      const bpm = Math.round(peaks * 12);
      if (bpm >= 40 && bpm <= 150) {
        return { signalQuality: "good" as const, heartRate: bpm };
      }
    }

    return { signalQuality: "good" as const, heartRate: null };
  }, [ecgData]);

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
  const totalSamples = totalBatches * 1000; // ~1000 samples per batch

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
      {signalQuality === "poor" && (
        <Card className="border-yellow-500 bg-yellow-50 dark:bg-yellow-950">
          <CardContent className="py-4">
            <div className="flex items-start gap-3">
              <AlertTriangle className="h-6 w-6 text-yellow-600 flex-shrink-0 mt-0.5" />
              <div>
                <h3 className="font-semibold text-yellow-800 dark:text-yellow-200">
                  Poor Signal Quality - Check Electrode Connection
                </h3>
                <p className="text-sm text-yellow-700 dark:text-yellow-300 mt-1">
                  The ECG signal appears to be clipping or noisy. This usually
                  means:
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
              {signalQuality === "good" ? (
                <CheckCircle2 className="h-5 w-5 text-green-500" />
              ) : signalQuality === "poor" ? (
                <AlertTriangle className="h-5 w-5 text-yellow-500" />
              ) : (
                <Wifi className="h-5 w-5 text-muted-foreground" />
              )}
              <span className="text-lg font-bold capitalize">
                {signalQuality === "good"
                  ? "Good"
                  : signalQuality === "poor"
                    ? "Poor"
                    : "---"}
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

      {/* ECG Channels - Full Width Charts */}
      <div className="space-y-6">
        {session.channels.map((channel) => (
          <Card key={channel}>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle className="text-lg flex items-center gap-2">
                    <Activity className="h-5 w-5 text-green-500" />
                    {channel} Channel
                  </CardTitle>
                  <CardDescription>
                    Real-time waveform at 1000 Hz | Y-axis: ADC value (0-1023) |
                    X-axis: Time (seconds)
                  </CardDescription>
                </div>
                {channel.toUpperCase() === "ECG" && heartRate && (
                  <div className="flex items-center gap-2 bg-red-50 dark:bg-red-950 px-3 py-1.5 rounded-lg">
                    <Heart className="h-5 w-5 text-red-500 animate-pulse" />
                    <span className="text-xl font-bold text-red-600 dark:text-red-400">
                      {heartRate}
                    </span>
                    <span className="text-sm text-red-500">BPM</span>
                  </div>
                )}
              </div>
            </CardHeader>
            <CardContent className="pt-0">
              <LiveECGWaveform
                sessionId={sessionId}
                channel={channel}
                sampleRate={100}
                displaySeconds={5}
                height={350}
                ecgData={ecgData ?? []}
              />
            </CardContent>
          </Card>
        ))}
      </div>

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
