import { describe, expect, it } from "vitest";
import { estimateHrMax, zoneCeilingBpm } from "./medicalParameters";

const PI_TANAKA_BY_AGE = [
  201, 200, 200, 199, 198, 198, 197, 196, 195, 195, 194, 193, 193, 192, 191,
  190, 190, 189, 188, 188, 187, 186, 186, 185, 184, 184, 183, 182, 181, 181,
  180, 179, 179, 178, 177, 176, 176, 175, 174, 174, 173, 172, 172, 171, 170,
  170, 169, 168, 167, 167, 166, 165, 165, 164, 163, 162, 162, 161, 160, 160,
  159, 158, 158, 157, 156, 156, 155, 154, 153, 153, 152, 151, 151, 150, 149,
  148, 148, 147, 146, 146, 145, 144, 144, 143, 142, 142, 141, 140, 139, 139,
  138,
];

describe("shared medical parameters", () => {
  it("matches the Pi's Tanaka estimate for every supported age", () => {
    PI_TANAKA_BY_AGE.forEach((expected, offset) => {
      expect(estimateHrMax(offset + 10, "tanaka"), `age ${offset + 10}`).toBe(
        expected,
      );
    });
  });

  it.each([
    [25, 190],
    [45, 176],
    [65, 162],
    [85, 148],
  ])("uses Python's even result at the exact half for age %i", (age, expected) => {
    expect(estimateHrMax(age, "tanaka")).toBe(expected);
  });

  it("does not estimate outside whole ages 10 through 100", () => {
    expect(estimateHrMax(9, "tanaka")).toBeNull();
    expect(estimateHrMax(100.5, "tanaka")).toBeNull();
    expect(estimateHrMax(101, "tanaka")).toBeNull();
  });

  it("floors the medical fraction of a measured or estimated maximum", () => {
    expect(zoneCeilingBpm(0.9, 181)).toBe(162);
  });
});
