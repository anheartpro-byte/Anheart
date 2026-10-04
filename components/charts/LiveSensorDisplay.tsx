"use client";

import { useState, useMemo } from "react";
import { ECGChart, EDAChart, SpO2Chart, RespChart } from "./SensorCharts";

interface ChannelMetrics {
  heartRate?: number;
  hrv?: number;
  respRate?: number;
  scrCount?: number;
  activations?: number;
  pulse?: number;
  quality?: string;
}

interface SensorBatch {
  timestamp: number;
  sampleRate?: number;
  samples: Array<{
    channel: string;
    values: number[];
    unit?: string;
  }>;
  metrics?: Record<string, ChannelMetrics>;
}

interface LiveSensorDisplayProps {
  /** Session ID for resetting state */
  sessionId: string;
  /** Incoming data batches from Convex query */
  ecgData: SensorBatch[];
  /** Sample rate from session config */
  sampleRate?: number;
  /** Channels to display */
  channels: string[];
}

/**
 * Live sensor display component that processes incoming data
 * and renders appropriate charts for each sensor type
 */
export function LiveSensorDisplay({
  sessionId,
  ecgData,
  sampleRate = 100,
  channels,
}: LiveSensorDisplayProps) {
  const maxSamples = sampleRate * 60; // Keep 60 seconds of data
  const [buffer, setBuffer] = useState<{
    readonly sessionId: string;
    readonly batches: SensorBatch[] | null;
    readonly maxSamples: number;
    readonly timestamp: number;
    readonly channels: Record<string, number[]>;
  }>({ sessionId, batches: null, maxSamples, timestamp: 0, channels: {} });

  // Preserve samples that have left the rolling query, without committing a
  // stale chart before an effect runs. React retries this render before its children.
  if (
    buffer.sessionId !== sessionId ||
    buffer.batches !== ecgData ||
    buffer.maxSamples !== maxSamples
  ) {
    const sameSession = buffer.sessionId === sessionId;
    const previousTimestamp = sameSession ? buffer.timestamp : 0;
    let timestamp = previousTimestamp;
    // Only this local accumulator is mutable; stored sample arrays are copied.
    const updated: Record<string, number[]> = sameSession
      ? { ...buffer.channels }
      : {};

    for (const batch of ecgData.filter((b) => b.timestamp > previousTimestamp)) {
      for (const sample of batch.samples) {
        const channelKey = sample.channel.toUpperCase();
        updated[channelKey] = [
          ...(updated[channelKey] ?? []),
          ...sample.values,
        ];
      }
      timestamp = Math.max(timestamp, batch.timestamp);
    }
    setBuffer({
      sessionId,
      batches: ecgData,
      maxSamples,
      timestamp,
      channels: Object.fromEntries(
        Object.entries(updated).map(([channel, values]) => [
          channel,
          values.slice(-maxSamples),
        ]),
      ),
    });
  }
  const channelData = buffer.channels;

  // Latest on-device metrics per channel (from the most recent batch that has them).
  const latestMetrics = useMemo(() => {
    const result: Record<string, ChannelMetrics> = {};
    for (const batch of ecgData) {
      if (!batch.metrics) continue;
      for (const [ch, m] of Object.entries(batch.metrics)) {
        result[ch.toUpperCase()] = m;
      }
    }
    return result;
  }, [ecgData]);

  // Data from the Pi is already treated (filtered, mV), charts must not re-filter.
  const isTreated = useMemo(
    () => ecgData.some((b) => b.samples.some((s) => s.unit)),
    [ecgData],
  );

  // Render chart for each channel based on type
  const renderChannelChart = (channel: string) => {
    const upperChannel = channel.toUpperCase();
    const data = channelData[upperChannel] || [];
    const metrics = latestMetrics[upperChannel];

    switch (upperChannel) {
      case "ECG":
        return (
          <ECGChart
            key={channel}
            data={data}
            sampleRate={sampleRate}
            displaySeconds={5}
            height={280}
            showMetrics={true}
            preFiltered={isTreated}
            heartRate={metrics ? (metrics.heartRate ?? null) : undefined}
            hrv={metrics ? (metrics.hrv ?? null) : undefined}
          />
        );

      case "EDA":
      case "GSR":
        return (
          <EDAChart
            key={channel}
            data={data}
            sampleRate={sampleRate}
            displaySeconds={60}
            height={200}
            showMetrics={true}
          />
        );

      case "SPO2":
      case "PPG":
        return (
          <SpO2Chart
            key={channel}
            data={data}
            sampleRate={sampleRate}
            displaySeconds={30}
            height={200}
            showMetrics={true}
          />
        );

      case "RESP":
      case "RESPIRATION":
        return (
          <RespChart
            key={channel}
            data={data}
            sampleRate={sampleRate}
            displaySeconds={30}
            height={200}
            showMetrics={true}
          />
        );

      case "EMG":
        // EMG uses similar display to ECG
        return (
          <ECGChart
            key={channel}
            data={data}
            sampleRate={sampleRate}
            displaySeconds={5}
            height={200}
            showMetrics={false}
            preFiltered={isTreated}
          />
        );

      case "LUX":
      case "LIGHT":
        // Light sensor - simple line chart (no ECG band-pass filtering)
        return (
          <ECGChart
            key={channel}
            data={data}
            sampleRate={sampleRate}
            displaySeconds={30}
            height={150}
            showMetrics={false}
            filter={false}
          />
        );

      default:
        // Default to ECG-style display
        return (
          <ECGChart
            key={channel}
            data={data}
            sampleRate={sampleRate}
            displaySeconds={5}
            height={200}
            showMetrics={false}
            preFiltered={isTreated}
          />
        );
    }
  };

  // Sort channels: ECG first, then SpO2, RESP, EDA, others
  const sortedChannels = useMemo(() => {
    const priority: Record<string, number> = {
      ECG: 0,
      SPO2: 1,
      RESP: 2,
      EDA: 3,
      EMG: 4,
      LUX: 5,
    };
    return [...channels].sort((a, b) => {
      const pA = priority[a.toUpperCase()] ?? 99;
      const pB = priority[b.toUpperCase()] ?? 99;
      return pA - pB;
    });
  }, [channels]);

  return (
    <div className="space-y-4">
      {sortedChannels.map((channel) => renderChannelChart(channel))}
    </div>
  );
}
