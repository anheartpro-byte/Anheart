"use client";

import { useMemo } from "react";
import { useTranslations } from "next-intl";
import {
  Area,
  AreaChart,
  Line,
  LineChart,
  XAxis,
  YAxis,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { computeHeartRate, filterEcg } from "@/lib/ecg";

// ============================================
// SHARED UTILITIES
// ============================================

interface SensorDataPoint {
  time: number;
  value: number;
}

/**
 * Downsample data for performance while preserving peaks
 */
function downsamplePreservingPeaks(
  data: number[],
  targetPoints: number,
  sampleRate: number,
): SensorDataPoint[] {
  if (data.length <= targetPoints) {
    return data.map((value, i) => ({ time: i / sampleRate, value }));
  }

  const result: SensorDataPoint[] = [];
  const chunkSize = Math.floor(data.length / targetPoints);

  for (let i = 0; i < data.length; i += chunkSize) {
    const chunk = data.slice(i, i + chunkSize);
    const min = Math.min(...chunk);
    const max = Math.max(...chunk);
    const avg = chunk.reduce((a, b) => a + b, 0) / chunk.length;

    // Keep the point that deviates most from average (preserves peaks)
    const value = Math.abs(max - avg) > Math.abs(avg - min) ? max : min;
    result.push({ time: i / sampleRate, value });
  }

  return result;
}

/**
 * Detect peaks in signal (for R-peaks, SCR peaks, breath peaks)
 */
function detectPeaks(
  data: number[],
  minDistance: number,
  threshold?: number,
): number[] {
  const peaks: number[] = [];
  const mean = data.reduce((a, b) => a + b, 0) / data.length;
  const effectiveThreshold = threshold ?? mean * 1.2;

  for (let i = minDistance; i < data.length - minDistance; i++) {
    if (data[i] < effectiveThreshold) continue;

    let isPeak = true;
    for (let j = i - minDistance; j <= i + minDistance; j++) {
      if (j !== i && data[j] >= data[i]) {
        isPeak = false;
        break;
      }
    }
    if (isPeak) {
      peaks.push(i);
    }
  }

  return peaks;
}

// ============================================
// ECG CHART - Heart Activity
// ============================================

interface ECGChartProps {
  data: number[];
  sampleRate?: number;
  displaySeconds?: number;
  height?: number;
  showMetrics?: boolean;
  /** Apply the ECG band-pass filter to the displayed waveform (default true). */
  filter?: boolean;
  /** Data is already treated on-device (skip client filtering). */
  preFiltered?: boolean;
  /** Heart rate from on-device metrics; when provided, used instead of client detection. */
  heartRate?: number | null;
  /** HRV (ms) from on-device metrics. */
  hrv?: number | null;
}

export function ECGChart({
  data,
  sampleRate = 100,
  displaySeconds = 5,
  height = 250,
  showMetrics = true,
  filter = true,
  preFiltered = false,
  heartRate,
  hrv,
}: ECGChartProps) {
  const t = useTranslations("sensors");

  // Heart-rate metrics. Prefer on-device values (computed by the Pi with BioSPPy);
  // otherwise fall back to the client detector for legacy raw sessions.
  const metrics = useMemo(() => {
    if (heartRate !== undefined) {
      return { heartRate: heartRate ?? 0, hrv: hrv ?? 0, rPeaks: [] as number[] };
    }
    const result = computeHeartRate(data, sampleRate);
    return {
      heartRate: result.bpm ?? 0,
      hrv: result.hrv ?? 0,
      rPeaks: result.rPeaks,
    };
  }, [data, sampleRate, heartRate, hrv]);

  // Prepare chart data. Treated data is already filtered on-device; only filter
  // here for legacy raw sessions so hum / baseline wander don't hide the QRS.
  const chartData = useMemo(() => {
    const maxSamples = sampleRate * displaySeconds;
    const samples = data.slice(-maxSamples);
    const display = filter && !preFiltered ? filterEcg(samples, sampleRate) : samples;
    return downsamplePreservingPeaks(display, 500, sampleRate);
  }, [data, sampleRate, displaySeconds, filter, preFiltered]);

  const yDomain = useMemo((): [number, number] => {
    if (chartData.length === 0) return [0, 1023];
    const values = chartData.map((d) => d.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = (max - min) * 0.15 || 50;
    return [Math.floor(min - padding), Math.ceil(max + padding)];
  }, [chartData]);

  const getHeartRateColor = (hr: number) => {
    if (hr === 0) return "text-muted-foreground";
    if (hr < 60) return "text-blue-500";
    if (hr <= 100) return "text-green-500";
    if (hr <= 120) return "text-yellow-500";
    return "text-red-500";
  };

  const getHeartRateStatus = (hr: number) => {
    if (hr === 0) return t("noSignal");
    if (hr < 60) return t("ecg.bradycardia");
    if (hr <= 100) return t("normal");
    if (hr <= 120) return t("ecg.elevated");
    return t("ecg.tachycardia");
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-lg flex items-center gap-2">
            <span className="text-2xl">❤️</span> {t("ecg.title")}
          </CardTitle>
          {showMetrics && (
            <div className="flex items-center gap-4">
              <div className="text-right">
                <div
                  className={cn(
                    "text-2xl font-bold",
                    getHeartRateColor(metrics.heartRate),
                  )}
                >
                  {metrics.heartRate || "--"}{" "}
                  <span className="text-sm font-normal">{t("bpm")}</span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {getHeartRateStatus(metrics.heartRate)}
                </div>
              </div>
              <div className="text-right border-l pl-4">
                <div className="text-lg font-semibold text-purple-500">
                  {metrics.hrv || "--"}{" "}
                  <span className="text-sm font-normal">ms</span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {t("ecg.hrv")}
                </div>
              </div>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {chartData.length === 0 ? (
          <div className="flex items-center justify-center h-[200px] text-muted-foreground">
            {t("ecg.waiting")}
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={height}>
            <LineChart
              data={chartData}
              margin={{ top: 5, right: 5, left: 0, bottom: 5 }}
            >
              <defs>
                <linearGradient id="ecgGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#22c55e" stopOpacity={0.3} />
                  <stop offset="100%" stopColor="#22c55e" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="#333"
                opacity={0.3}
              />
              <XAxis
                dataKey="time"
                type="number"
                domain={[0, displaySeconds]}
                tickFormatter={(v) => `${v.toFixed(0)}s`}
                stroke="#666"
                fontSize={11}
              />
              <YAxis domain={yDomain} stroke="#666" fontSize={11} width={45} />
              <Line
                type="monotone"
                dataKey="value"
                stroke="#22c55e"
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
        <div className="flex justify-between text-xs text-muted-foreground mt-2">
          <span>{t("ecg.scale")}</span>
          <span>{t("ecg.rPeaks", { count: metrics.rPeaks.length })}</span>
        </div>
      </CardContent>
    </Card>
  );
}

// ============================================
// EDA CHART - Stress / Skin Conductance
// ============================================

interface EDAChartProps {
  data: number[];
  sampleRate?: number;
  displaySeconds?: number;
  height?: number;
  showMetrics?: boolean;
}

export function EDAChart({
  data,
  sampleRate = 100,
  displaySeconds = 30,
  height = 200,
  showMetrics = true,
}: EDAChartProps) {
  const t = useTranslations("sensors");

  // EDA metrics
  const metrics = useMemo(() => {
    if (data.length < sampleRate) {
      return { scl: 0, scrCount: 0, stressLevel: "unknown" };
    }

    // Skin Conductance Level (SCL) - tonic component (baseline)
    const scl = data.reduce((a, b) => a + b, 0) / data.length;

    // Detect SCR (Skin Conductance Responses) - phasic peaks
    // SCRs indicate emotional/stress responses
    const minDistance = Math.floor(sampleRate * 1); // 1 second minimum between SCRs
    const mean = data.reduce((a, b) => a + b, 0) / data.length;
    const scrPeaks = detectPeaks(data, minDistance, mean * 1.05);
    const scrCount = scrPeaks.length;

    // Determine stress level based on SCL and SCR frequency
    let stressLevel: "low" | "moderate" | "high" | "unknown" = "unknown";
    const scrPerMinute = (scrCount / data.length) * sampleRate * 60;

    if (scrPerMinute < 2) stressLevel = "low";
    else if (scrPerMinute < 5) stressLevel = "moderate";
    else stressLevel = "high";

    return { scl: Math.round(scl), scrCount, stressLevel, scrPerMinute };
  }, [data, sampleRate]);

  const chartData = useMemo(() => {
    const maxSamples = sampleRate * displaySeconds;
    const samples = data.slice(-maxSamples);
    return downsamplePreservingPeaks(samples, 300, sampleRate);
  }, [data, sampleRate, displaySeconds]);

  const yDomain = useMemo((): [number, number] => {
    if (chartData.length === 0) return [0, 1023];
    const values = chartData.map((d) => d.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = (max - min) * 0.1 || 20;
    return [Math.floor(min - padding), Math.ceil(max + padding)];
  }, [chartData]);

  const getStressColor = (level: string) => {
    switch (level) {
      case "low":
        return "bg-green-500";
      case "moderate":
        return "bg-yellow-500";
      case "high":
        return "bg-red-500";
      default:
        return "bg-gray-500";
    }
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-lg flex items-center gap-2">
            <span className="text-2xl">😰</span> {t("eda.title")}
          </CardTitle>
          {showMetrics && (
            <div className="flex items-center gap-4">
              <div className="text-right">
                <div className="text-xl font-bold text-cyan-500">
                  {metrics.scl || "--"}{" "}
                  <span className="text-sm font-normal">µS</span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {t("eda.skinConductance")}
                </div>
              </div>
              <div className="text-right border-l pl-4">
                <div className="text-lg font-semibold">
                  {metrics.scrCount}{" "}
                  <span className="text-sm font-normal">
                    {t("eda.scrUnit")}
                  </span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {t("eda.responses")}
                </div>
              </div>
              <Badge
                className={cn("ml-2", getStressColor(metrics.stressLevel))}
              >
                {t(`eda.stress.${metrics.stressLevel}`)}
              </Badge>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {chartData.length === 0 ? (
          <div className="flex items-center justify-center h-[150px] text-muted-foreground">
            {t("eda.waiting")}
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={height}>
            <AreaChart
              data={chartData}
              margin={{ top: 5, right: 5, left: 0, bottom: 5 }}
            >
              <defs>
                <linearGradient id="edaGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#06b6d4" stopOpacity={0.4} />
                  <stop offset="100%" stopColor="#06b6d4" stopOpacity={0.05} />
                </linearGradient>
              </defs>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="#333"
                opacity={0.3}
              />
              <XAxis
                dataKey="time"
                type="number"
                domain={[0, displaySeconds]}
                tickFormatter={(v) => `${v.toFixed(0)}s`}
                stroke="#666"
                fontSize={11}
              />
              <YAxis domain={yDomain} stroke="#666" fontSize={11} width={45} />
              <ReferenceLine
                y={metrics.scl}
                stroke="#06b6d4"
                strokeDasharray="5 5"
              />
              <Area
                type="monotone"
                dataKey="value"
                stroke="#06b6d4"
                strokeWidth={2}
                fill="url(#edaGradient)"
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
        <div className="text-xs text-muted-foreground mt-2">
          {t("eda.legend")}
        </div>
      </CardContent>
    </Card>
  );
}

// ============================================
// SpO2 CHART - Blood Oxygen
// ============================================

interface SpO2ChartProps {
  data: number[];
  sampleRate?: number;
  displaySeconds?: number;
  height?: number;
  showMetrics?: boolean;
}

export function SpO2Chart({
  data,
  sampleRate = 100,
  displaySeconds = 30,
  height = 200,
  showMetrics = true,
}: SpO2ChartProps) {
  const t = useTranslations("sensors");

  // SpO2 metrics - convert ADC to approximate percentage
  const metrics = useMemo(() => {
    if (data.length < sampleRate) {
      return { spo2: 0, pulse: 0, perfusionIndex: 0 };
    }

    // BITalino PPG gives raw ADC values
    // We need to calculate SpO2 from red and infrared LED signals
    // For single-channel PPG, we can estimate from signal characteristics

    // Detect pulse peaks for heart rate
    const minDistance = Math.floor((sampleRate * 400) / 1000); // 400ms min = 150 BPM max
    const mean = data.reduce((a, b) => a + b, 0) / data.length;
    const pulsePeaks = detectPeaks(data, minDistance, mean);

    // Pulse rate from peaks
    let pulse = 0;
    if (pulsePeaks.length >= 2) {
      const intervals = [];
      for (let i = 1; i < pulsePeaks.length; i++) {
        intervals.push((pulsePeaks[i] - pulsePeaks[i - 1]) / sampleRate);
      }
      const avgInterval =
        intervals.reduce((a, b) => a + b, 0) / intervals.length;
      pulse = Math.round(60 / avgInterval);
    }

    // SpO2 estimation (simplified - real calculation needs dual wavelength)
    // Using signal quality as proxy
    const min = Math.min(...data);
    const max = Math.max(...data);
    const amplitude = max - min;
    const perfusionIndex = (amplitude / mean) * 100;

    // Estimate SpO2 (95-100% for good signal, lower if poor)
    // This is a simplified estimation - real SpO2 needs red/IR ratio
    let spo2 = 0;
    if (amplitude > 50 && perfusionIndex > 1) {
      spo2 = Math.min(100, Math.max(90, 98 - (100 - perfusionIndex) * 0.1));
    }

    return {
      spo2: Math.round(spo2),
      pulse,
      perfusionIndex: Math.round(perfusionIndex * 10) / 10,
    };
  }, [data, sampleRate]);

  const chartData = useMemo(() => {
    const maxSamples = sampleRate * displaySeconds;
    const samples = data.slice(-maxSamples);
    return downsamplePreservingPeaks(samples, 300, sampleRate);
  }, [data, sampleRate, displaySeconds]);

  const yDomain = useMemo((): [number, number] => {
    if (chartData.length === 0) return [0, 1023];
    const values = chartData.map((d) => d.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = (max - min) * 0.15 || 30;
    return [Math.floor(min - padding), Math.ceil(max + padding)];
  }, [chartData]);

  const getSpO2Color = (spo2: number) => {
    if (spo2 === 0) return "text-muted-foreground";
    if (spo2 >= 95) return "text-green-500";
    if (spo2 >= 90) return "text-yellow-500";
    return "text-red-500";
  };

  const getSpO2Status = (spo2: number) => {
    if (spo2 === 0) return t("noSignal");
    if (spo2 >= 95) return t("normal");
    if (spo2 >= 90) return t("spo2.low");
    return t("spo2.critical");
  };

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-lg flex items-center gap-2">
            <span className="text-2xl">🩸</span> {t("spo2.title")}
          </CardTitle>
          {showMetrics && (
            <div className="flex items-center gap-4">
              <div className="text-right">
                <div
                  className={cn(
                    "text-2xl font-bold",
                    getSpO2Color(metrics.spo2),
                  )}
                >
                  {metrics.spo2 || "--"}
                  <span className="text-sm font-normal">%</span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {getSpO2Status(metrics.spo2)}
                </div>
              </div>
              <div className="text-right border-l pl-4">
                <div className="text-lg font-semibold text-rose-500">
                  {metrics.pulse || "--"}{" "}
                  <span className="text-sm font-normal">{t("bpm")}</span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {t("spo2.pulse")}
                </div>
              </div>
              <div className="text-right border-l pl-4">
                <div className="text-lg font-semibold text-amber-500">
                  {metrics.perfusionIndex || "--"}
                  <span className="text-sm font-normal">%</span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {t("spo2.perfusionIndex")}
                </div>
              </div>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {chartData.length === 0 ? (
          <div className="flex items-center justify-center h-[150px] text-muted-foreground">
            {t("spo2.waiting")}
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={height}>
            <AreaChart
              data={chartData}
              margin={{ top: 5, right: 5, left: 0, bottom: 5 }}
            >
              <defs>
                <linearGradient id="spo2Gradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#f43f5e" stopOpacity={0.4} />
                  <stop offset="100%" stopColor="#f43f5e" stopOpacity={0.05} />
                </linearGradient>
              </defs>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="#333"
                opacity={0.3}
              />
              <XAxis
                dataKey="time"
                type="number"
                domain={[0, displaySeconds]}
                tickFormatter={(v) => `${v.toFixed(0)}s`}
                stroke="#666"
                fontSize={11}
              />
              <YAxis domain={yDomain} stroke="#666" fontSize={11} width={45} />
              <Area
                type="monotone"
                dataKey="value"
                stroke="#f43f5e"
                strokeWidth={2}
                fill="url(#spo2Gradient)"
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
        <div className="text-xs text-muted-foreground mt-2">
          {t("spo2.legend")}
        </div>
      </CardContent>
    </Card>
  );
}

// ============================================
// RESPIRATION CHART - Breathing
// ============================================

interface RespChartProps {
  data: number[];
  sampleRate?: number;
  displaySeconds?: number;
  height?: number;
  showMetrics?: boolean;
}

export function RespChart({
  data,
  sampleRate = 100,
  displaySeconds = 30,
  height = 200,
  showMetrics = true,
}: RespChartProps) {
  const t = useTranslations("sensors");

  // Respiration metrics
  const metrics = useMemo(() => {
    if (data.length < sampleRate * 5) {
      return { breathRate: 0, breathDepth: 0, regularity: 0 };
    }

    // Detect breath peaks (inspiration peaks)
    // Normal breathing: 12-20 breaths/min = 3-5 seconds between breaths
    const minDistance = Math.floor(sampleRate * 2); // 2 seconds minimum
    const mean = data.reduce((a, b) => a + b, 0) / data.length;
    const breathPeaks = detectPeaks(data, minDistance, mean);

    // Breath rate from peaks
    let breathRate = 0;
    let regularity = 0;
    if (breathPeaks.length >= 2) {
      const intervals: number[] = [];
      for (let i = 1; i < breathPeaks.length; i++) {
        intervals.push((breathPeaks[i] - breathPeaks[i - 1]) / sampleRate);
      }
      const avgInterval =
        intervals.reduce((a, b) => a + b, 0) / intervals.length;
      breathRate = Math.round(60 / avgInterval);

      // Calculate regularity (coefficient of variation)
      const std = Math.sqrt(
        intervals.reduce((sum, v) => sum + Math.pow(v - avgInterval, 2), 0) /
          intervals.length,
      );
      regularity = Math.round((1 - std / avgInterval) * 100);
    }

    // Breath depth (amplitude)
    const min = Math.min(...data);
    const max = Math.max(...data);
    const breathDepth = max - min;

    return { breathRate, breathDepth, regularity, breathPeaks };
  }, [data, sampleRate]);

  const chartData = useMemo(() => {
    const maxSamples = sampleRate * displaySeconds;
    const samples = data.slice(-maxSamples);
    return downsamplePreservingPeaks(samples, 300, sampleRate);
  }, [data, sampleRate, displaySeconds]);

  const yDomain = useMemo((): [number, number] => {
    if (chartData.length === 0) return [0, 1023];
    const values = chartData.map((d) => d.value);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = (max - min) * 0.15 || 30;
    return [Math.floor(min - padding), Math.ceil(max + padding)];
  }, [chartData]);

  const getBreathRateStatus = (rate: number) => {
    if (rate === 0)
      return { text: t("noSignal"), color: "text-muted-foreground" };
    if (rate < 12) return { text: t("resp.slow"), color: "text-blue-500" };
    if (rate <= 20) return { text: t("normal"), color: "text-green-500" };
    if (rate <= 25) return { text: t("resp.fast"), color: "text-yellow-500" };
    return { text: t("resp.rapid"), color: "text-red-500" };
  };

  const status = getBreathRateStatus(metrics.breathRate);

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-lg flex items-center gap-2">
            <span className="text-2xl">🫁</span> {t("resp.title")}
          </CardTitle>
          {showMetrics && (
            <div className="flex items-center gap-4">
              <div className="text-right">
                <div className={cn("text-2xl font-bold", status.color)}>
                  {metrics.breathRate || "--"}
                  <span className="text-sm font-normal">
                    {" "}
                    {t("resp.perMinute")}
                  </span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {status.text}
                </div>
              </div>
              <div className="text-right border-l pl-4">
                <div className="text-lg font-semibold text-indigo-500">
                  {metrics.regularity || "--"}
                  <span className="text-sm font-normal">%</span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {t("resp.regularity")}
                </div>
              </div>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {chartData.length === 0 ? (
          <div className="flex items-center justify-center h-[150px] text-muted-foreground">
            {t("resp.waiting")}
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={height}>
            <AreaChart
              data={chartData}
              margin={{ top: 5, right: 5, left: 0, bottom: 5 }}
            >
              <defs>
                <linearGradient id="respGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#8b5cf6" stopOpacity={0.4} />
                  <stop offset="100%" stopColor="#8b5cf6" stopOpacity={0.05} />
                </linearGradient>
              </defs>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke="#333"
                opacity={0.3}
              />
              <XAxis
                dataKey="time"
                type="number"
                domain={[0, displaySeconds]}
                tickFormatter={(v) => `${v.toFixed(0)}s`}
                stroke="#666"
                fontSize={11}
              />
              <YAxis domain={yDomain} stroke="#666" fontSize={11} width={45} />
              <Area
                type="natural"
                dataKey="value"
                stroke="#8b5cf6"
                strokeWidth={2}
                fill="url(#respGradient)"
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
        <div className="text-xs text-muted-foreground mt-2">
          {t("resp.legend")}
        </div>
      </CardContent>
    </Card>
  );
}

// ============================================
// MULTI-SENSOR DASHBOARD
// ============================================

interface MultiSensorDashboardProps {
  ecgData?: number[];
  edaData?: number[];
  spo2Data?: number[];
  respData?: number[];
  sampleRate?: number;
}

export function MultiSensorDashboard({
  ecgData = [],
  edaData = [],
  spo2Data = [],
  respData = [],
  sampleRate = 100,
}: MultiSensorDashboardProps) {
  return (
    <div className="space-y-4">
      {/* Primary vital - ECG always on top */}
      {ecgData.length > 0 && (
        <ECGChart data={ecgData} sampleRate={sampleRate} height={280} />
      )}

      {/* Secondary vitals in grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {spo2Data.length > 0 && (
          <SpO2Chart data={spo2Data} sampleRate={sampleRate} height={180} />
        )}
        {respData.length > 0 && (
          <RespChart data={respData} sampleRate={sampleRate} height={180} />
        )}
      </div>

      {/* Stress indicator - full width */}
      {edaData.length > 0 && (
        <EDAChart
          data={edaData}
          sampleRate={sampleRate}
          displaySeconds={60}
          height={180}
        />
      )}
    </div>
  );
}

// Export individual chart components
export {
  type ECGChartProps,
  type EDAChartProps,
  type SpO2ChartProps,
  type RespChartProps,
};
