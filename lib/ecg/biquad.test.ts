import { describe, expect, it } from "vitest";
import {
  butterHighpass4,
  butterLowpass4,
  filtfilt,
  highpassCoeffs,
  notchCoeffs,
} from "./biquad";

describe("ANH-71 biquad endpoint initialization", () => {
  it.each([250, 1000])(
    "removes a constant baseline in the quality high-pass at %i Hz",
    (sampleRate) => {
      // Given a constant ADC input and the quality gate's actual section.
      const samples = Array<number>(sampleRate).fill(512);
      const coefficients = highpassCoeffs(0.5, sampleRate, Math.SQRT1_2);
      // When both filtering passes process that constant input.
      const filtered = filtfilt(coefficients, samples);
      // Then startup does not introduce a varying DC artifact.
      expect(Math.max(...filtered.map(Math.abs))).toBeLessThan(1e-8);
    },
  );

  it.each([250, 1000])(
    "preserves constants in the Hamilton low-pass at %i Hz",
    (sampleRate) => {
      // Given the shared low-pass's constant-input equilibrium.
      const samples = Array<number>(sampleRate).fill(512);
      // When the actual two-section cascade filters the input.
      const filtered = butterLowpass4(samples, 25, sampleRate);
      // Then both sections retain the constant within roundoff tolerance.
      expect(
        Math.max(...filtered.map((sample) => Math.abs(sample - 512))),
      ).toBeLessThan(1e-8);
    },
  );

  it.each([250, 1000])(
    "removes constants in the Hamilton high-pass at %i Hz",
    (sampleRate) => {
      // Given the shared high-pass's constant-input equilibrium.
      const samples = Array<number>(sampleRate).fill(512);
      // When the actual two-section cascade filters the input.
      const filtered = butterHighpass4(samples, 3, sampleRate);
      // Then each section removes DC without startup transients.
      expect(Math.max(...filtered.map(Math.abs))).toBeLessThan(1e-8);
    },
  );

  it.each([250, 1000])(
    "preserves constants through the mains notch at %i Hz",
    (sampleRate) => {
      // Given a constant input and the actual 50Hz notch section.
      const samples = Array<number>(sampleRate).fill(512);
      const coefficients = notchCoeffs(50, sampleRate);
      // When both filtering passes process the signal.
      const filtered = filtfilt(coefficients, samples);
      // Then the notch's unity DC gain remains intact.
      expect(
        Math.max(...filtered.map((sample) => Math.abs(sample - 512))),
      ).toBeLessThan(1e-8);
    },
  );

  it("keeps an empty input empty", () => {
    // Given no samples to establish an endpoint.
    const coefficients = highpassCoeffs(0.5, 1000, Math.SQRT1_2);
    // When the public internal filter receives the empty input.
    const filtered = filtfilt(coefficients, []);
    // Then no fabricated endpoint or sample is introduced.
    expect(filtered).toEqual([]);
  });

  it("uses each pass's own endpoint for a nonconstant input", () => {
    // Given y=(x+y_previous)/2: forward values are 2, 3, 5.5 at equilibrium.
    const coefficients = { b0: 0.5, b1: 0, b2: 0, a1: -0.5, a2: 0 } as const;
    const samples = [2, 4, 8];
    // When the backward pass starts from the forward endpoint of 5.5.
    const filtered = filtfilt(coefficients, samples);
    // Then the analytic three-sample response matches both endpoint states.
    expect(filtered).toEqual([3.125, 4.25, 5.5]);
  });
});
