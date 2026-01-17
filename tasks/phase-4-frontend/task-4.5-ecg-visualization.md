# Task 4.5: ECG Visualization Component

## Objective

Create a real-time ECG visualization component using lightweight-charts for both live monitoring and historical playback.

## Dependencies

- Task 4.4 (Session Management UI) completed

---

## Acceptance Criteria

### ECGWaveform Component

- [ ] Renders ECG data as line chart
- [ ] Smooth real-time updates
- [ ] Auto-scroll for live view
- [ ] Grid lines at standard intervals
- [ ] Y-axis shows amplitude
- [ ] X-axis shows time

### Live View Features

- [ ] Subscribes to real-time data
- [ ] 10-second visible window
- [ ] Heart rate indicator
- [ ] Connection status indicator
- [ ] "Delayed view" badge for gestionnaire

### Historical View Features

- [ ] Zoom and pan controls
- [ ] Time range selector
- [ ] Load data in chunks (pagination)
- [ ] Smooth navigation

### Multi-Channel Support

- [ ] Toggle between channels
- [ ] Stacked view option
- [ ] Color-coded channels

### Performance

- [ ] Handles 1000 samples/second
- [ ] Smooth animation
- [ ] Memory efficient (clears old data)

---

## Implementation

```bash
npm install lightweight-charts
```

```typescript
// components/ecg/ECGWaveform.tsx
"use client";

import { useEffect, useRef, useState } from "react";
import { createChart, IChartApi, ISeriesApi, LineData } from "lightweight-charts";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";

interface ECGWaveformProps {
  sessionId: Id<"sessions">;
  isLive?: boolean;
  channel?: string;
}

export function ECGWaveform({
  sessionId,
  isLive = false,
  channel = "ECG",
}: ECGWaveformProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const [heartRate, setHeartRate] = useState<number | null>(null);

  // Subscribe to real-time data
  const recentData = useQuery(
    api.ecgData.getRecentEcgData,
    isLive ? { sessionId, seconds: 10 } : "skip"
  );

  // Initialize chart
  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      width: chartContainerRef.current.clientWidth,
      height: 300,
      layout: {
        background: { color: "#ffffff" },
        textColor: "#333",
      },
      grid: {
        vertLines: { color: "#f0f0f0" },
        horzLines: { color: "#f0f0f0" },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: true,
      },
      rightPriceScale: {
        borderColor: "#cccccc",
      },
    });

    const lineSeries = chart.addLineSeries({
      color: "#2563eb",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
    });

    chartRef.current = chart;
    seriesRef.current = lineSeries;

    // Handle resize
    const handleResize = () => {
      if (chartContainerRef.current) {
        chart.applyOptions({
          width: chartContainerRef.current.clientWidth,
        });
      }
    };

    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, []);

  // Update data
  useEffect(() => {
    if (!seriesRef.current || !recentData) return;

    const chartData: LineData[] = [];

    for (const batch of recentData) {
      const channelData = batch.samples.find(
        (s) => s.channel.toUpperCase() === channel.toUpperCase()
      );
      if (channelData) {
        // Each batch is 1 second of data
        const samplesPerBatch = channelData.values.length;
        const msPerSample = 1000 / samplesPerBatch;

        channelData.values.forEach((value, index) => {
          const time = batch.timestamp + index * msPerSample;
          chartData.push({
            time: time / 1000, // Convert to seconds
            value,
          });
        });
      }
    }

    // Sort by time
    chartData.sort((a, b) => (a.time as number) - (b.time as number));

    // Update series
    seriesRef.current.setData(chartData);

    // Auto-scroll to latest
    if (isLive && chartRef.current && chartData.length > 0) {
      chartRef.current.timeScale().scrollToRealTime();
    }

    // Calculate heart rate from visible data (simplified)
    if (chartData.length > 100) {
      const estimatedHR = estimateHeartRate(chartData.map((d) => d.value));
      setHeartRate(estimatedHR);
    }
  }, [recentData, channel, isLive]);

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
      {/* Header */}
      <div className="flex justify-between items-center mb-4">
        <div className="flex items-center gap-4">
          <span className="font-medium">{channel}</span>
          {isLive && (
            <span className="flex items-center gap-1 text-green-600">
              <span className="w-2 h-2 bg-green-500 rounded-full animate-pulse" />
              Live
            </span>
          )}
        </div>
        {heartRate && (
          <div className="text-right">
            <span className="text-2xl font-bold text-red-600">{heartRate}</span>
            <span className="text-sm text-gray-500 ml-1">BPM</span>
          </div>
        )}
      </div>

      {/* Chart */}
      <div ref={chartContainerRef} className="w-full" />
    </div>
  );
}

// Simple heart rate estimation (peaks per 10 seconds * 6)
function estimateHeartRate(values: number[]): number | null {
  if (values.length < 100) return null;

  const max = Math.max(...values);
  const min = Math.min(...values);
  const threshold = min + (max - min) * 0.6;

  let peaks = 0;
  for (let i = 1; i < values.length - 1; i++) {
    if (
      values[i] > threshold &&
      values[i] > values[i - 1] &&
      values[i] > values[i + 1]
    ) {
      peaks++;
    }
  }

  // Assuming 10 seconds of data
  const bpm = peaks * 6;
  return bpm > 30 && bpm < 220 ? Math.round(bpm) : null;
}
```

```typescript
// app/[locale]/dashboard/sessions/[id]/live/page.tsx
"use client";

import { useQuery, useMutation } from "convex/react";
import { api } from "@/convex/_generated/api";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { ECGWaveform } from "@/components/ecg/ECGWaveform";
import { Id } from "@/convex/_generated/dataModel";
import { useState } from "react";

export default function LiveSessionPage({
  params,
}: {
  params: { locale: string; id: string };
}) {
  const t = useTranslations("sessions");
  const router = useRouter();
  const [ending, setEnding] = useState(false);

  const sessionId = params.id as Id<"sessions">;
  const session = useQuery(api.sessions.getSession, { sessionId });
  const user = useQuery(api.users.getCurrentUser);
  const endSession = useMutation(api.sessions.endSession);

  const isDelayed =
    user?.role === "gestionnaire" || user?.role === "technician";

  const handleEndSession = async () => {
    if (!confirm("Are you sure you want to end this session?")) return;

    setEnding(true);
    try {
      await endSession({ sessionId });
      router.push(`/${params.locale}/dashboard/sessions/${sessionId}`);
    } catch (error: any) {
      alert(error.message);
      setEnding(false);
    }
  };

  if (!session) {
    return <div>Loading...</div>;
  }

  if (session.status !== "active") {
    return (
      <div className="text-center py-12">
        <p className="text-lg text-gray-500">Session is not active</p>
        <a
          href={`/${params.locale}/dashboard/sessions/${sessionId}`}
          className="text-blue-600 hover:underline mt-4 inline-block"
        >
          View Session Details
        </a>
      </div>
    );
  }

  return (
    <div>
      {/* Header */}
      <div className="flex justify-between items-center mb-6">
        <div>
          <h1 className="text-2xl font-bold">
            {session.patient.firstName} {session.patient.lastName}
          </h1>
          <p className="text-gray-500">
            {session.machine.name} - Started{" "}
            {new Date(session.startedAt).toLocaleTimeString()}
          </p>
        </div>
        <div className="flex items-center gap-4">
          {isDelayed && (
            <span className="bg-yellow-100 text-yellow-800 px-3 py-1 rounded-full text-sm">
              5s Delay
            </span>
          )}
          <button
            onClick={handleEndSession}
            disabled={ending}
            className="bg-red-600 text-white px-4 py-2 rounded-md hover:bg-red-700 disabled:opacity-50"
          >
            {ending ? "Ending..." : t("end")}
          </button>
        </div>
      </div>

      {/* ECG Charts */}
      <div className="space-y-4">
        {session.channels.map((channel) => (
          <ECGWaveform
            key={channel}
            sessionId={sessionId}
            isLive={true}
            channel={channel}
          />
        ))}
      </div>
    </div>
  );
}
```

```typescript
// components/ecg/HistoricalECG.tsx
"use client";

import { useState } from "react";
import { useQuery } from "convex/react";
import { api } from "@/convex/_generated/api";
import { Id } from "@/convex/_generated/dataModel";
import { createChart } from "lightweight-charts";
import { useEffect, useRef } from "react";

interface HistoricalECGProps {
  sessionId: Id<"sessions">;
}

export function HistoricalECG({ sessionId }: HistoricalECGProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const summary = useQuery(api.sessionSummaries.getSummaryWithEcg, {
    sessionId,
  });

  useEffect(() => {
    if (!chartContainerRef.current || !summary?.downsampledEcg) return;

    const chart = createChart(chartContainerRef.current, {
      width: chartContainerRef.current.clientWidth,
      height: 200,
      layout: {
        background: { color: "#f9fafb" },
        textColor: "#333",
      },
      handleScroll: { mouseWheel: true, pressedMouseMove: true },
      handleScale: { mouseWheel: true, pinch: true },
    });

    const lineSeries = chart.addLineSeries({
      color: "#2563eb",
      lineWidth: 1,
    });

    const data = summary.downsampledEcg.map((d) => ({
      time: d.timestamp / 1000,
      value: d.value,
    }));

    lineSeries.setData(data);
    chart.timeScale().fitContent();

    return () => chart.remove();
  }, [summary]);

  if (!summary) return <div>Loading chart...</div>;

  return (
    <div className="bg-white rounded-lg shadow p-4">
      <h3 className="font-medium mb-4">ECG Overview</h3>
      <div ref={chartContainerRef} className="w-full" />
      <p className="text-sm text-gray-500 mt-2">
        Scroll to zoom, drag to pan
      </p>
    </div>
  );
}
```

---

## Testing Steps

1. Open live session as patient - verify real-time updates
2. Open as gestionnaire - verify 5s delay badge
3. Watch for 1 minute - verify smooth scrolling
4. Check heart rate display - verify reasonable values
5. View completed session - verify historical chart
6. Zoom/pan historical chart - verify works smoothly
7. Test multi-channel session - verify all channels display
