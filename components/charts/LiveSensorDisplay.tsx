"use client";

import { useRef, useEffect, useState, useMemo } from "react";
import { ECGChart, EDAChart, SpO2Chart, RespChart } from "./SensorCharts";

interface SensorBatch {
  timestamp: number;
  samples: Array<{
    channel: string;
    values: number[];
  }>;
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
  // Separate state for each channel's accumulated data
  const [channelData, setChannelData] = useState<Record<string, number[]>>({});
  const lastTimestampRef = useRef<number>(0);
  const maxSamples = sampleRate * 60; // Keep 60 seconds of data

  // Process incoming batches and extract channel data
  useEffect(() => {
    if (!ecgData || ecgData.length === 0) return;

    // Find new batches
    const newBatches = ecgData.filter(
      (batch) => batch.timestamp > lastTimestampRef.current,
    );

    if (newBatches.length === 0) return;

    // Extract samples for each channel
    const newChannelData: Record<string, number[]> = {};

    for (const batch of newBatches) {
      for (const sample of batch.samples) {
        const channelKey = sample.channel.toUpperCase();
        if (!newChannelData[channelKey]) {
          newChannelData[channelKey] = [];
        }
        newChannelData[channelKey].push(...sample.values);
      }
      lastTimestampRef.current = Math.max(
        lastTimestampRef.current,
        batch.timestamp,
      );
    }

    // Update state with new data
    setChannelData((prev) => {
      const updated = { ...prev };
      for (const [channel, values] of Object.entries(newChannelData)) {
        const existing = updated[channel] || [];
        const combined = [...existing, ...values];
        // Keep only last N samples
        updated[channel] = combined.slice(-maxSamples);
      }
      return updated;
    });
  }, [ecgData, maxSamples]);

  // Reset when session changes
  useEffect(() => {
    setChannelData({});
    lastTimestampRef.current = 0;
  }, [sessionId]);

  // Render chart for each channel based on type
  const renderChannelChart = (channel: string) => {
    const upperChannel = channel.toUpperCase();
    const data = channelData[upperChannel] || [];

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
          />
        );

      case "LUX":
      case "LIGHT":
        // Light sensor - simple line chart
        return (
          <ECGChart
            key={channel}
            data={data}
            sampleRate={sampleRate}
            displaySeconds={30}
            height={150}
            showMetrics={false}
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
