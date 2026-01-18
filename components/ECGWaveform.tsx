"use client";

import { useMemo, useRef, useEffect, useState } from "react";
import { Area, AreaChart, XAxis, YAxis, CartesianGrid } from "recharts";
import { ChartContainer, ChartConfig } from "@/components/ui/chart";

/**
 * ECG Data Explanation:
 *
 * BITalino sends data at 1000 Hz (1000 samples per second).
 * Each sample is a 10-bit ADC value (0-1023).
 *
 * Y-Axis: ADC value (0-1023)
 *   - This represents the raw voltage reading from the ECG sensor
 *   - 512 is typically the baseline (no signal)
 *   - Values above/below 512 represent the ECG waveform deflections
 *   - NOT heart rate in BPM!
 *
 * X-Axis: Time in seconds
 *   - At 1000 Hz, each sample = 1ms = 0.001 seconds
 *   - 1000 samples = 1 second of data
 *   - We display 5-10 seconds of data in a scrolling window
 */

interface ECGWaveformProps {
  /** Raw ADC values from BITalino (0-1023) */
  data: number[];
  /** Channel name (ECG, EMG, etc.) */
  channel: string;
  /** Sample rate in Hz (default 1000) */
  sampleRate?: number;
  /** How many seconds to display (default 5) */
  displaySeconds?: number;
  /** Chart height in pixels */
  height?: number;
}

const chartConfig = {
  ecg: {
    label: "ECG",
    color: "hsl(142, 76%, 36%)", // Green color for ECG
  },
} satisfies ChartConfig;

export function ECGWaveform({
  data,
  channel,
  sampleRate = 100,
  displaySeconds = 5,
  height = 300,
}: ECGWaveformProps) {
  // Calculate how many samples to display
  const maxSamples = sampleRate * displaySeconds;

  // Downsample for performance - we don't need 5000 points for a smooth line
  // Display ~500 points max (100 points per second is enough for visualization)
  const downsampleFactor = Math.max(1, Math.floor(sampleRate / 100));

  // Prepare chart data - take the last N samples and downsample
  const chartData = useMemo(() => {
    const samples = data.slice(-maxSamples);

    // Downsample: take every Nth sample
    const downsampled: { time: number; value: number }[] = [];
    for (let i = 0; i < samples.length; i += downsampleFactor) {
      downsampled.push({
        time: i / sampleRate, // Time in seconds
        value: samples[i],
      });
    }

    return downsampled;
  }, [data, maxSamples, sampleRate, downsampleFactor]);

  // Calculate Y-axis domain based on actual data
  const yDomain = useMemo((): [number, number] => {
    if (chartData.length === 0) return [0, 1023];

    const values = chartData.map((d) => d.value);
    const min = Math.min(...values);
    const max = Math.max(...values);

    // Add 10% padding
    const range = max - min || 100;
    const padding = range * 0.1;

    return [
      Math.max(0, Math.floor(min - padding)),
      Math.min(1023, Math.ceil(max + padding)),
    ];
  }, [chartData]);

  if (chartData.length === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-lg border border-dashed bg-muted/20"
        style={{ height }}
      >
        <div className="text-center text-muted-foreground">
          <p className="text-sm">Waiting for {channel} data...</p>
          <p className="text-xs mt-1">Connect BITalino to see waveform</p>
        </div>
      </div>
    );
  }

  return (
    <ChartContainer config={chartConfig} className="w-full" style={{ height }}>
      <AreaChart
        data={chartData}
        margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
      >
        <defs>
          <linearGradient
            id={`gradient-${channel}`}
            x1="0"
            y1="0"
            x2="0"
            y2="1"
          >
            <stop
              offset="0%"
              stopColor="hsl(142, 76%, 36%)"
              stopOpacity={0.3}
            />
            <stop
              offset="100%"
              stopColor="hsl(142, 76%, 36%)"
              stopOpacity={0.05}
            />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
        <XAxis
          dataKey="time"
          type="number"
          domain={[0, displaySeconds]}
          tickCount={displaySeconds + 1}
          tickFormatter={(v) => `${v.toFixed(0)}s`}
          className="text-xs"
          tick={{ fill: "hsl(var(--muted-foreground))" }}
          axisLine={{ stroke: "hsl(var(--border))" }}
        />
        <YAxis
          domain={yDomain}
          tickCount={5}
          className="text-xs"
          tick={{ fill: "hsl(var(--muted-foreground))" }}
          axisLine={{ stroke: "hsl(var(--border))" }}
          width={50}
          tickFormatter={(v) => v.toFixed(0)}
        />
        <Area
          type="monotone"
          dataKey="value"
          stroke="hsl(142, 76%, 36%)"
          strokeWidth={1.5}
          fill={`url(#gradient-${channel})`}
          isAnimationActive={false}
          dot={false}
        />
      </AreaChart>
    </ChartContainer>
  );
}

interface LiveECGWaveformProps {
  sessionId: string;
  channel: string;
  sampleRate?: number;
  displaySeconds?: number;
  height?: number;
  ecgData: Array<{
    timestamp: number;
    samples: Array<{
      channel: string;
      values: number[];
    }>;
  }>;
}

export function LiveECGWaveform({
  sessionId,
  channel,
  sampleRate = 100,
  displaySeconds = 5,
  height = 300,
  ecgData,
}: LiveECGWaveformProps) {
  const [allSamples, setAllSamples] = useState<number[]>([]);
  const lastTimestampRef = useRef<number>(0);

  // Process incoming ECG data batches - this effect syncs external streaming data
  useEffect(() => {
    if (!ecgData || ecgData.length === 0) return;

    // Find new batches (after last processed timestamp)
    const newBatches = ecgData.filter(
      (batch) => batch.timestamp > lastTimestampRef.current,
    );

    if (newBatches.length === 0) return;

    // Extract samples for this channel from new batches
    const newSamples: number[] = [];
    for (const batch of newBatches) {
      const channelData = batch.samples.find(
        (s) => s.channel.toUpperCase() === channel.toUpperCase(),
      );
      if (channelData) {
        newSamples.push(...channelData.values);
      }
      lastTimestampRef.current = Math.max(
        lastTimestampRef.current,
        batch.timestamp,
      );
    }

    if (newSamples.length > 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- Syncing streaming data from external source
      setAllSamples((prev) => {
        // Keep only last N seconds of samples (plus buffer)
        const maxSamples = sampleRate * (displaySeconds + 2);
        const combined = [...prev, ...newSamples];
        return combined.slice(-maxSamples);
      });
    }
  }, [ecgData, channel, sampleRate, displaySeconds]);

  // Reset when session changes - legitimate use of setState in effect for cleanup
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- Cleanup effect when session changes
    setAllSamples([]);
    lastTimestampRef.current = 0;
  }, [sessionId]);

  return (
    <ECGWaveform
      data={allSamples}
      channel={channel}
      sampleRate={sampleRate}
      displaySeconds={displaySeconds}
      height={height}
    />
  );
}
