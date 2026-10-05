import { describe, expect, it } from "vitest";
import {
  formatSampleRates,
  recordingCoverage,
  summarizeRecording,
  type RecordedBatch,
} from "./stats";

/** A treated batch as the Pi sends it; `sampleRate: null` leaves the rate out. */
function treatedBatch(
  valueCount: number,
  sampleRate: number | null = 250,
): RecordedBatch {
  return {
    sampleRate: sampleRate ?? undefined,
    samples: [
      {
        channel: "ECG",
        values: Array<number>(valueCount).fill(0.1),
        unit: "mV",
      },
    ],
  };
}

function rawBatch(valueCount: number, sampleRate?: number): RecordedBatch {
  return {
    sampleRate,
    samples: [{ channel: "ECG", values: Array<number>(valueCount).fill(512) }],
  };
}

describe("ANH-123 recording statistics come from the recorded data", () => {
  it("counts a treated 250 Hz recording instead of multiplying the batch count", () => {
    // Given 126 one-second batches of treated ECG, as the Pi sends them.
    const batches = Array.from({ length: 126 }, () => treatedBatch(250));
    // When the statistics are counted from those batches.
    const stats = summarizeRecording(batches, 1000);
    // Then the total is what was stored, not 126 x 1000 (session page) or 126 x 100 (PDF).
    expect(stats.totalSamples).toBe(31_500);
    expect(stats.totalSamples).not.toBe(batches.length * 1000);
    expect(stats.totalSamples).not.toBe(batches.length * 100);
    expect(stats.batchCount).toBe(126);
  });

  it("reads the sample rate from the batches, not from a constant or the machine config", () => {
    // Given treated batches resampled to 250 Hz on a machine acquiring at 1000 Hz.
    const batches = [treatedBatch(250), treatedBatch(250)];
    // When the session's acquisition rate is offered as a fallback.
    const stats = summarizeRecording(batches, 1000);
    // Then only the rate the batches carry is reported.
    expect(stats.sampleRates).toEqual([250]);
    expect(stats.treated).toBe(true);
  });

  it("counts batches of unequal length exactly", () => {
    // Given a recording whose last batch is shorter than the others.
    const batches = [treatedBatch(250), treatedBatch(250), treatedBatch(137)];
    // When the samples are counted.
    const stats = summarizeRecording(batches);
    // Then every stored value counts once, with no rounding to a batch size.
    expect(stats.totalSamples).toBe(637);
    expect(stats.samplesByChannel).toEqual({ ECG: 637 });
    expect(stats.channelCount).toBe(1);
  });

  it("counts each channel separately and sums them", () => {
    // Given two channels of different lengths in the same batches.
    const batch: RecordedBatch = {
      sampleRate: 250,
      samples: [
        { channel: "ECG", values: Array<number>(250).fill(0), unit: "mV" },
        { channel: "RESP", values: Array<number>(25).fill(0), unit: "%" },
      ],
    };
    // When three such batches are summarized.
    const stats = summarizeRecording([batch, batch, batch]);
    // Then the per-channel counts and their sum are both exact,
    // and the total is known to add up two series.
    expect(stats.samplesByChannel).toEqual({ ECG: 750, RESP: 75 });
    expect(stats.totalSamples).toBe(825);
    expect(stats.channelCount).toBe(2);
  });

  it.each(["constructor", "toString", "__proto__"])(
    "counts a channel named %s like any other",
    (channel) => {
      // Given a channel whose name is also an inherited object property.
      const batch: RecordedBatch = {
        sampleRate: 250,
        samples: [{ channel, values: [0.1, 0.2, 0.3], unit: "mV" }],
      };
      // When two such batches are summarized.
      const stats = summarizeRecording([batch, batch]);
      // Then its values are counted as numbers under its own name.
      expect(Object.entries(stats.samplesByChannel)).toEqual([[channel, 6]]);
      expect(stats.totalSamples).toBe(6);
      expect(stats.channelCount).toBe(1);
    },
  );

  it("uses the session rate only for raw batches that carry none", () => {
    // Given legacy raw ADC batches stored without a rate.
    const batches = [rawBatch(1000), rawBatch(1000)];
    // When the session recorded its acquisition rate.
    const stats = summarizeRecording(batches, 1000);
    // Then that recorded rate describes the raw values.
    expect(stats.sampleRates).toEqual([1000]);
    expect(stats.treated).toBe(false);
    expect(stats.totalSamples).toBe(2000);
  });

  it("reports no rate for treated batches that carry none", () => {
    // Given treated batches stored without a rate (the field is optional).
    const batches = [treatedBatch(250, null), treatedBatch(250, null)];
    // When the acquisition rate is the only other rate known.
    const stats = summarizeRecording(batches, 1000);
    // Then no rate is invented: 1000 Hz would be wrong for resampled data.
    expect(stats.sampleRates).toEqual([]);
    expect(formatSampleRates(stats.sampleRates)).toBeNull();
    expect(stats.totalSamples).toBe(500);
  });

  it("reports no rate for raw batches when the session recorded none", () => {
    // Given raw batches and a session created before the rate was stored.
    const stats = summarizeRecording([rawBatch(1000)]);
    // Then the rate stays unknown instead of defaulting to a constant.
    expect(stats.sampleRates).toEqual([]);
  });

  it("reports every distinct rate when batches disagree", () => {
    // Given a recording whose output rate changed between batches.
    const batches = [
      treatedBatch(500, 500),
      treatedBatch(250),
      treatedBatch(250),
    ];
    // When the rates are collected.
    const stats = summarizeRecording(batches);
    // Then both are shown, ascending, rather than the last one winning.
    expect(stats.sampleRates).toEqual([250, 500]);
    expect(formatSampleRates(stats.sampleRates)).toBe("250 / 500");
  });

  it.each([0, -250, Number.NaN, Number.POSITIVE_INFINITY])(
    "ignores the non-physical batch rate %d",
    (sampleRate) => {
      // Given a treated batch with a rate no ADC can have.
      const stats = summarizeRecording([treatedBatch(250, sampleRate)]);
      // Then it is not reported, and the samples are still counted.
      expect(stats.sampleRates).toEqual([]);
      expect(stats.totalSamples).toBe(250);
    },
  );

  it("takes no rate from a batch without values", () => {
    // Given a batch that announces a rate but stores nothing.
    const stats = summarizeRecording([treatedBatch(0, 250)]);
    // Then it adds neither samples nor a rate.
    expect(stats.totalSamples).toBe(0);
    expect(stats.sampleRates).toEqual([]);
    expect(stats.samplesByChannel).toEqual({ ECG: 0 });
    expect(stats.channelCount).toBe(0);
  });

  it("returns zeros for a recording without batches", () => {
    // Given a session that stored no ECG data.
    const stats = summarizeRecording([], 1000);
    // Then nothing is counted and no rate is claimed.
    expect(stats).toEqual({
      batchCount: 0,
      samplesByChannel: {},
      channelCount: 0,
      totalSamples: 0,
      sampleRates: [],
      treated: false,
    });
  });

  it("does not mutate the batches it counts", () => {
    // Given batches the caller keeps for charting.
    const batches = [treatedBatch(3), treatedBatch(2)];
    const snapshot = JSON.stringify(batches);
    // When they are summarized.
    summarizeRecording(batches, 1000);
    // Then they are left untouched.
    expect(JSON.stringify(batches)).toBe(snapshot);
  });
});

describe("ANH-123 partial recordings are flagged, not passed off as totals", () => {
  it("flags a count over fewer batches than the server holds", () => {
    // Given 200 loaded batches out of the 540 the server counted.
    // Then the count over them is not the session total.
    expect(recordingCoverage(200, 540)).toBe("partial");
  });

  it("accepts a count over every batch", () => {
    // Given as many loaded batches as the server counted.
    expect(recordingCoverage(126, 126)).toBe("complete");
  });

  it.each([undefined, null])(
    "does not call a count complete while the server total is %s",
    (totalBatches) => {
      // Given 200 batches already loaded and statistics still loading (or refused).
      // Then the count is neither a total nor a labelled partial: the screen waits.
      expect(recordingCoverage(200, totalBatches)).toBe("unknown");
      expect(recordingCoverage(0, totalBatches)).toBe("unknown");
    },
  );

  it("accepts a recording that stored no batch at all", () => {
    // Given a session the server counted zero batches for.
    expect(recordingCoverage(0, 0)).toBe("complete");
  });

  it("does not flag when more batches arrived than the earlier server count", () => {
    // Given a live session whose batch list is fresher than its statistics.
    expect(recordingCoverage(12, 10)).toBe("complete");
  });
});

describe("ANH-123 sample rate display", () => {
  it("formats a single recorded rate without a unit", () => {
    expect(formatSampleRates([250])).toBe("250");
  });

  it("returns null when no rate was recorded", () => {
    expect(formatSampleRates([])).toBeNull();
  });
});
