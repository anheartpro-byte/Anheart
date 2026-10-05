/**
 * Recording statistics counted from the stored batches themselves.
 *
 * Nothing here multiplies a batch count by an assumed rate or batch size: a
 * batch carries its own `sampleRate` (treated data, about 250 Hz) and however
 * many values the Pi actually sent, so both are read from the data.
 */

export interface RecordedBatch {
  /** Rate of the stored values in Hz; absent on legacy raw batches. */
  sampleRate?: number;
  samples: ReadonlyArray<{
    channel: string;
    values: ReadonlyArray<number>;
    /** Physical unit of the values; present only on on-device treated data. */
    unit?: string;
  }>;
}

export interface RecordingStats {
  /** How many batches the figures below were counted from. */
  batchCount: number;
  /** Values stored per channel, in first-seen order. */
  samplesByChannel: Record<string, number>;
  /** Channels that actually hold values: how many series `totalSamples` sums. */
  channelCount: number;
  /** Values stored across every channel. */
  totalSamples: number;
  /** Distinct rates (Hz) of the stored values, ascending; empty when the data does not say. */
  sampleRates: number[];
  /** At least one channel carries a physical unit, i.e. was treated on-device. */
  treated: boolean;
}

function isRate(value: number | undefined): value is number {
  return value !== undefined && Number.isFinite(value) && value > 0;
}

/**
 * Count samples and collect sample rates from recorded batches.
 *
 * `sessionSampleRate` is the acquisition rate copied onto the session when it
 * started. It only stands in for RAW batches that carry no rate of their own:
 * those were stored as acquired. A treated batch without a rate was resampled
 * to a rate nobody recorded, so it contributes none rather than a guess.
 */
export function summarizeRecording(
  batches: ReadonlyArray<RecordedBatch>,
  sessionSampleRate?: number,
): RecordingStats {
  // A Map, not an object: a channel named "constructor" must count like any other.
  const samplesByChannel = new Map<string, number>();
  const rates = new Set<number>();
  let totalSamples = 0;
  let treated = false;

  for (const batch of batches) {
    let batchTreated = false;
    let batchHasValues = false;
    for (const sample of batch.samples) {
      if (sample.unit) batchTreated = true;
      if (sample.values.length > 0) batchHasValues = true;
      samplesByChannel.set(
        sample.channel,
        (samplesByChannel.get(sample.channel) ?? 0) + sample.values.length,
      );
      totalSamples += sample.values.length;
    }
    if (batchTreated) treated = true;
    if (!batchHasValues) continue;
    if (isRate(batch.sampleRate)) {
      rates.add(batch.sampleRate);
    } else if (!batchTreated && isRate(sessionSampleRate)) {
      rates.add(sessionSampleRate);
    }
  }

  return {
    batchCount: batches.length,
    samplesByChannel: Object.fromEntries(samplesByChannel),
    channelCount: Array.from(samplesByChannel.values()).filter(
      (count) => count > 0,
    ).length,
    totalSamples,
    sampleRates: Array.from(rates).sort((a, b) => a - b),
    treated,
  };
}

/**
 * How much of the recording the batches at hand cover.
 *
 * - `unknown`: the server has not said how many batches exist, so a count over
 *   the loaded ones cannot be called the session total yet;
 * - `partial`: the server holds more batches than were loaded;
 * - `complete`: every batch the server counted was loaded.
 */
export type RecordingCoverage = "unknown" | "partial" | "complete";

export function recordingCoverage(
  loadedBatches: number,
  totalBatches: number | undefined | null,
): RecordingCoverage {
  if (totalBatches === undefined || totalBatches === null) return "unknown";
  return totalBatches > loadedBatches ? "partial" : "complete";
}

/** "250" or "250 / 1000" for display; null when the data records no rate. */
export function formatSampleRates(rates: ReadonlyArray<number>): string | null {
  return rates.length > 0 ? rates.join(" / ") : null;
}
