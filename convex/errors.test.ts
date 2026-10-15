/// <reference types="vite/client" />
/**
 * ANH-194 EX-5: the dashboard's refusals carry a stable code from
 * `convex/lib/errors.ts`, and the site has a French and an English sentence
 * for each of them (`errors.<code>` in `messages/*.json`).
 */
import { ConvexError } from "convex/values";
import { describe, expect, it } from "vitest";
import en from "../messages/en.json";
import fr from "../messages/fr.json";
import {
  DASHBOARD_ERROR_CODES,
  DASHBOARD_ERRORS,
  refusal,
} from "./lib/errors";

// Every deployable source of the backend, as text.
const sources = import.meta.glob(
  [
    "./**/*.ts",
    "!./_generated/**",
    "!./**/*.test.ts",
    "!./**/*.fixtures.ts",
    "!./**/*.setup.ts",
    "!./**/*.matrix.ts",
  ],
  { eager: true, query: "?raw", import: "default" },
) as Record<string, string>;

const catalogs = { fr: fr.errors, en: en.errors } as Record<
  string,
  Record<string, string>
>;

describe("ANH-194 EX-5 the table of error codes", () => {
  it("has a French and an English sentence for every code", () => {
    for (const [language, catalog] of Object.entries(catalogs)) {
      const missing = DASHBOARD_ERROR_CODES.filter(
        (code) => (catalog[code] ?? "").trim() === "",
      );
      expect(missing, language).toEqual([]);
    }
  });

  it("leaves no translation without its code", () => {
    for (const [language, catalog] of Object.entries(catalogs)) {
      expect(Object.keys(catalog).sort(), language).toEqual(
        [...DASHBOARD_ERROR_CODES].sort(),
      );
    }
  });

  it("holds codes a translation key can name: one lowercase token each", () => {
    for (const code of DASHBOARD_ERROR_CODES) {
      expect(code).toMatch(/^[a-z][a-z0-9_]*$/);
      expect(DASHBOARD_ERRORS[code].length).toBeGreaterThan(0);
    }
  });

  it("refuses with a ConvexError whose data is the code alone", () => {
    const error = refusal("machine_in_session");

    expect(error).toBeInstanceOf(ConvexError);
    expect(error.data).toBe("machine_in_session");
    expect(error.message).toBe("machine_in_session");
  });

  it("is where every code thrown by the backend comes from", () => {
    // A code written in place, `new ConvexError("some_code")` or
    // `new ConvexError({ code: "some_code", ... })`, must be in the table
    // too: otherwise the site would show the bare code.
    const thrown = new Set<string>();
    for (const text of Object.values(sources)) {
      for (const match of text.matchAll(
        /new ConvexError(?:<[^>]*>)?\(\s*(?:\{\s*code:\s*)?"([a-z][a-z0-9_]*)"/g,
      )) {
        thrown.add(match[1]);
      }
    }
    // The account limiter's code is one of them (ANH-166).
    expect(thrown).toContain("rate_limited");
    expect(
      [...thrown].filter((code) => !(code in DASHBOARD_ERRORS)),
    ).toEqual([]);
  });

  it("is used for every refusal of the machines and accounts functions", () => {
    // Convex masks a plain Error in production: these two modules no longer
    // throw one, except the heartbeat, which answers the machine, not a page.
    expect(plainErrors("./users.ts")).toEqual([]);
    expect(plainErrors("./machines.ts")).toEqual(["Machine not found"]);
    expect(sources["./machines.ts"]).toContain(
      "// Read by the heartbeat route, which answers the machine",
    );
  });
});

/** The texts of the plain `Error`s a source throws (`throw new Error("...")`, `throw Error("...")`). */
function plainErrors(path: string): string[] {
  return [
    ...sources[path].matchAll(/throw (?:new )?Error\(\s*"([^"]*)"/g),
  ].map((match) => match[1]);
}

/** The texts of the `ConvexError`s a source throws with a sentence, not a code. */
function sentences(path: string): string[] {
  return [
    ...sources[path].matchAll(/new ConvexError(?:<[^>]*>)?\(\s*(["`])([^"`]*)\1/g),
  ]
    .map((match) => match[2])
    .filter((text) => !/^[a-z][a-z0-9_]*$/.test(text));
}

describe("ANH-90 no English sentence reaches the site", () => {
  it("signs in with codes: a refusal of the sign-in is read in production too", () => {
    // `requireAuth` and `getCurrentUserOrThrow` once threw plain Errors,
    // which Convex masks in production.
    expect(plainErrors("./lib/auth.ts")).toEqual([]);
    expect(sentences("./lib/auth.ts")).toEqual([]);
    for (const code of [
      "not_authenticated",
      "account_not_registered",
      "no_active_organization",
      "organization_not_synchronized",
      "unknown_organization_role",
      "malformed_organization_claim",
      "no_organization",
    ]) {
      expect(code in DASHBOARD_ERRORS).toBe(true);
    }
  });

  it("refuses with codes in the training, releases and records functions", () => {
    expect(sentences("./softwareReleases.ts")).toEqual([]);
    expect(plainErrors("./softwareReleases.ts")).toEqual([]);
    // The only plain Errors of the records module are its machine's routes'.
    expect(plainErrors("./recordStorage.ts")).toEqual([
      "Manifest pages are incomplete",
      "Invalid manifest page",
    ]);
    expect(sentences("./notifications.ts")).toEqual([]);
    expect(plainErrors("./notifications.ts")).toEqual([]);
    // What is left in training.ts is the rights and the busy machine of
    // `launchAutoSession`, which ANH-144 rewrites: converted after it.
    expect(sentences("./training.ts").sort()).toEqual(
      [
        "You can only launch a session for yourself",
        "You have not been given the right to launch sessions on this machine",
        "Not authorized to use this machine",
        "Not authorized to launch a session for this rider",
        "Machine is offline",
        "Machine is already in a session",
        "A session is already waiting for this machine",
        "Rider not found",
      ].sort(),
    );
  });
});
