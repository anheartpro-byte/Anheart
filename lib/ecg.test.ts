import { describe, expect, it } from "vitest";
import {
  adcToMillivolts,
  assessSignalQuality,
  computeHeartRate,
  filterEcg,
} from "./ecg";

function hum(sampleRate: number, offset: number): number[] {
  return Array.from({ length: sampleRate * 10 }, (_, i) =>
    Math.round(offset + 30 * Math.sin((2 * Math.PI * 50 * i) / sampleRate)),
  );
}

function pulses(sampleRate: number, offset: number): number[] {
  return Array.from({ length: sampleRate * 10 }, (_, i) => {
    const phase = (i % sampleRate) / sampleRate;
    const distance = (phase - 0.25) / 0.018;
    return Math.round(offset + 150 * Math.exp(-distance * distance));
  });
}

const cases = [
  { sampleRate: 250, offset: 64 },
  { sampleRate: 250, offset: 512 },
  { sampleRate: 250, offset: 960 },
  { sampleRate: 1000, offset: 64 },
  { sampleRate: 1000, offset: 512 },
  { sampleRate: 1000, offset: 960 },
] as const;

describe("ANH-71 legacy raw ECG noise rejection", () => {
  it.each(cases)(
    "rejects integer mains hum at $sampleRate Hz with ADC offset $offset",
    ({ sampleRate, offset }) => {
      // Given resolved 50Hz interference within the raw10-bit ADC range.
      const samples = hum(sampleRate, offset);
      // When the actual public quality gate evaluates the raw signal.
      const quality = assessSignalQuality(samples, sampleRate);
      // Then a constant ADC offset does not hide the interference.
      expect(quality).toBe("mains_dominated");
    },
  );

  it.each(cases)(
    "reports no heart metrics from mains hum at $sampleRate Hz, offset $offset",
    ({ sampleRate, offset }) => {
      // Given the same admissible raw interference, not a recorded human signal.
      const samples = hum(sampleRate, offset);
      // When the public detector receives no on-device heart metric.
      const result = computeHeartRate(samples, sampleRate);
      // Then interference cannot be reported as a heart rate or HRV.
      expect(result).toEqual({
        bpm: null,
        hrv: null,
        rPeaks: [],
        quality: "mains_dominated",
      });
    },
  );

  it.each([250, 1000])(
    "preserves a nominal one-second pulse train at %i Hz",
    (sampleRate) => {
      // Given an integer, in-range pulse fixture with a known one-second period.
      const samples = pulses(sampleRate, 512);
      // When the existing detector evaluates the synthetic fixture.
      const result = computeHeartRate(samples, sampleRate);
      // Then rejecting interference does not suppress the nominal signal.
      expect(result).toMatchObject({ bpm: 60, quality: "good" });
    },
  );

  it.each([250, 1000])(
    "keeps a flat raw lead unavailable at %i Hz",
    (sampleRate) => {
      // Given constant raw ADC values rather than a fabricated pulse.
      const samples = Array<number>(sampleRate * 10).fill(512);
      // When the public heart detector evaluates the flat input.
      const result = computeHeartRate(samples, sampleRate);
      // Then no heart metrics are returned.
      expect(result).toEqual({
        bpm: null,
        hrv: null,
        rPeaks: [],
        quality: "no_signal",
      });
    },
  );

  it("does not mutate raw samples during display filtering", () => {
    // Given the caller's retained ADC data.
    const samples = pulses(250, 512);
    const original = samples.slice();
    // When a separate display waveform is computed.
    const filtered = filterEcg(samples, 250);
    // Then source data stays intact and sample alignment is retained.
    expect(samples).toEqual(original);
    expect(filtered).toHaveLength(original.length);
  });

  it("maps the default10-bit ADC midpoint to zero millivolts", () => {
    // Given the documented sensor transfer midpoint.
    const adc = 512;
    // When the public transfer function converts it.
    const millivolts = adcToMillivolts(adc);
    // Then the unchanged transfer still produces the zero baseline.
    expect(millivolts).toBe(0);
  });
});
