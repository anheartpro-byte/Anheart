/**
 * Pure calculations shared by the dashboard backend and the signed medical
 * rules parsed by `raspberry-pi/src/medical.py`. Supported ages follow
 * `raspberry-pi/src/local_config.py`; the server stores every signed version
 * so these calculations can use the current registered formula.
 */
export type HrMaxFormula = "tanaka";

/** The medical values needed by the shared heart-rate calculations. */
export type MedicalParameters = Readonly<{
  zoneCeilingFraction: number;
  minRiderAge: number;
  hrMaxFormula: HrMaxFormula;
}>;

/** Estimate maximum heart rate; ties round to even, matching Python `round`. */
export function estimateHrMax(age: number, formula: HrMaxFormula): number | null {
  if (!Number.isInteger(age) || age < 10 || age > 100) return null;

  switch (formula) {
    case "tanaka": {
      const value = 208 - 0.7 * age;
      const lower = Math.floor(value);
      const fraction = value - lower;
      if (fraction < 0.5) return lower;
      if (fraction > 0.5) return lower + 1;
      return lower % 2 === 0 ? lower : lower + 1;
    }
  }
}

/** Floor the allowed fraction of a maximum heart rate to whole BPM. */
export function zoneCeilingBpm(fraction: number, hrMax: number): number {
  return Math.floor(fraction * hrMax);
}
