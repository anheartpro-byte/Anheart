/**
 * ANH-134: what a software release is, and which machine may receive it.
 *
 * Three components are versioned independently: the Raspberry Pi client
 * (`pi-X.Y.Z`), the Convex backend (`cloud-X.Y.Z`) and the site (`web-X.Y.Z`).
 * Only a Pi version carries a validation level, because only the Pi moves the
 * machine.
 *
 * This file holds the rule; it does not apply it. The machine registry
 * (ANH-147) and the remote update (ANH-116, ANH-168) call
 * `releaseAllowedOnMachine` before they offer or install a version.
 */

export const SOFTWARE_COMPONENTS = ["pi", "cloud", "web"] as const;
export type SoftwareComponent = (typeof SOFTWARE_COMPONENTS)[number];

/** How far a Pi version has been validated, lowest first. */
export const VALIDATION_LEVELS = [
  "bench",
  "auto_validated",
  "occupied_validated",
] as const;
export type ValidationLevel = (typeof VALIDATION_LEVELS)[number];

/**
 * How far a machine has been validated, lowest first: nothing yet, the bench
 * (M3), programmed sessions (M5), a person on board (M6). The registry
 * (ANH-147) stores this value per machine.
 */
export const MACHINE_VALIDATION_LEVELS = [
  "none",
  "bench",
  "auto",
  "occupied",
] as const;
export type MachineValidationLevel = (typeof MACHINE_VALIDATION_LEVELS)[number];

const REQUIRED_RELEASE_LEVEL: Record<
  MachineValidationLevel,
  ValidationLevel | null
> = {
  none: null,
  bench: "bench",
  auto: "auto_validated",
  occupied: "occupied_validated",
};

/** The lowest release level a machine in this state accepts (null: any). */
export function requiredReleaseLevel(
  machine: MachineValidationLevel,
): ValidationLevel | null {
  return REQUIRED_RELEASE_LEVEL[machine];
}

/**
 * True when a Pi version validated up to `release` may run on a machine
 * validated up to `machine`. A version with no recorded level (`undefined`) is
 * accepted only by a machine that has no validation to lose.
 */
export function releaseAllowedOnMachine(
  release: ValidationLevel | undefined,
  machine: MachineValidationLevel,
): boolean {
  const required = requiredReleaseLevel(machine);
  if (required === null) return true;
  if (release === undefined) return false;
  return (
    VALIDATION_LEVELS.indexOf(release) >= VALIDATION_LEVELS.indexOf(required)
  );
}

const CORE = "(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)";

/** True for the tag of a released version of `component`, e.g. `pi-0.1.0`. */
export function isReleaseVersion(
  component: SoftwareComponent,
  version: string,
): boolean {
  return new RegExp(`^${component}-${CORE}$`).test(version);
}
