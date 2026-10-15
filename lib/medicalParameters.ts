export type HrMaxFormula = "tanaka";

export type MedicalParameters = Readonly<{
  zoneCeilingFraction: number;
  minRiderAge: number;
  hrMaxFormula: HrMaxFormula;
}>;

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

export function zoneCeilingBpm(fraction: number, hrMax: number): number {
  return Math.floor(fraction * hrMax);
}
