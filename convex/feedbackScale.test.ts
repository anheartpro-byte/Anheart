/// <reference types="vite/client" />
/**
 * ANH-146: the scale of the rider's feedback, `contracts/feedback.json`, as
 * `convex/lib/feedback.ts` reads it, and what it accepts as a feedback.
 *
 * - the contract is read from the file and checked at load: two scales of
 *   eleven levels each labelled in French and English, three comforts, the
 *   provisional medical status, no threshold;
 * - M4: whole scores of the scale, a comfort of the list, a free text of 500
 *   characters at most once trimmed, empty or blank text is no text;
 * - the console's `detail` is strict JSON with closed keys.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import shared from "../contracts/feedback.json";
import {
  EDIT_WINDOW_MS,
  FEEDBACK_COMFORTS,
  FEEDBACK_CONTRACT,
  FEEDBACK_SUBMITTERS,
  FREE_TEXT_MAX_LENGTH,
  parseFeedbackContract,
  parseMachineFeedback,
  validateFeedback,
} from "./lib/feedback";

const valid = {
  nauseaScore: 3,
  dizzinessScore: 4,
  comfort: "acceptable",
} as const;

describe("ANH-146 the scale is contracts/feedback.json", () => {
  it("serves the file's scales, comforts, bounds and window", () => {
    expect(FEEDBACK_CONTRACT).toEqual(shared);
    expect(FEEDBACK_CONTRACT.provisional).toBe(true);
    expect(FEEDBACK_CONTRACT.medicalStatus).toMatch(/^\[MED\].*ANH-98/);
    expect(FREE_TEXT_MAX_LENGTH).toBe(500);
    expect(EDIT_WINDOW_MS).toBe(24 * 60 * 60 * 1000);
    expect(FEEDBACK_CONTRACT.comfort.map((item) => item.value)).toEqual([
      ...FEEDBACK_COMFORTS,
    ]);
    expect(FEEDBACK_CONTRACT.submittedBy).toEqual([...FEEDBACK_SUBMITTERS]);
  });

  it.each(["nausea", "dizziness"] as const)(
    "labels each of the eleven levels of %s in French and English",
    (name) => {
      const scale = FEEDBACK_CONTRACT.scales[name];
      expect([scale.min, scale.max]).toEqual([0, 10]);
      expect(scale.levels.map((level) => level.value)).toEqual(
        Array.from({ length: 11 }, (_, index) => index),
      );
      for (const { label } of [scale, ...scale.levels].map((item) =>
        "title" in item ? { label: item.title } : item,
      )) {
        expect(label.fr.trim()).not.toBe("");
        expect(label.en.trim()).not.toBe("");
      }
    },
  );

  it("names the scales as the ticket asks, with the accents of French", () => {
    expect(FEEDBACK_CONTRACT.scales.nausea.title.fr).toBe("Nausée");
    expect(FEEDBACK_CONTRACT.scales.dizziness.title.fr).toBe(
      "Malaise ou vertige",
    );
    expect(
      FEEDBACK_CONTRACT.scales.nausea.levels.map((level) => level.label.fr),
    ).toEqual([
      "Aucune",
      "Très légère",
      "Légère",
      "Légère à modérée",
      "Modérée",
      "Moyenne",
      "Assez forte",
      "Forte",
      "Très forte",
      "Extrême",
      "Maximale imaginable",
    ]);
    expect(FEEDBACK_CONTRACT.comfort.map((item) => item.label.fr)).toEqual([
      "Bon",
      "Acceptable",
      "Mauvais",
    ]);
  });

  it("states no medical threshold", () => {
    expect(JSON.stringify(shared)).not.toMatch(/threshold|seuil|alert/i);
  });

  const broken: [string, (contract: typeof shared) => unknown][] = [
    ["not an object", () => [shared]],
    ["an unknown key", (c) => ({ ...c, threshold: 5 })],
    ["a level missing", (c) => ({
      ...c,
      scales: {
        ...c.scales,
        nausea: { ...c.scales.nausea, levels: c.scales.nausea.levels.slice(1) },
      },
    })],
    ["levels out of order", (c) => ({
      ...c,
      scales: {
        ...c.scales,
        dizziness: {
          ...c.scales.dizziness,
          levels: [...c.scales.dizziness.levels].reverse(),
        },
      },
    })],
    ["an empty label", (c) => ({
      ...c,
      scales: {
        ...c.scales,
        nausea: { ...c.scales.nausea, title: { fr: " ", en: "Nausea" } },
      },
    })],
    ["min not below max", (c) => ({
      ...c,
      scales: { ...c.scales, nausea: { ...c.scales.nausea, max: 0 } },
    })],
    ["a comfort missing", (c) => ({ ...c, comfort: c.comfort.slice(0, 2) })],
    ["an unknown comfort", (c) => ({
      ...c,
      comfort: [...c.comfort.slice(0, 2), { value: "great", label: c.comfort[2].label }],
    })],
    ["a window that is not whole hours", (c) => ({ ...c, editWindowHours: 1.5 })],
    ["a text limit of zero", (c) => ({ ...c, freeTextMaxLength: 0 })],
    ["submitters in another order", (c) => ({
      ...c,
      submittedBy: ["operator", "rider"],
    })],
  ];

  it.each(broken)("refuses a contract with %s", (_name, change) => {
    expect(parseFeedbackContract(change(structuredClone(shared)))).toBeNull();
  });

  it("accepts the file itself", () => {
    expect(parseFeedbackContract(structuredClone(shared))).toEqual(shared);
  });
});

describe("ANH-146 M4 what a feedback is", () => {
  it.each([0, 10, 5])("accepts the score %s on both scales", (score) => {
    expect(
      validateFeedback({ ...valid, nauseaScore: score, dizzinessScore: score }),
    ).toEqual({ ...valid, nauseaScore: score, dizzinessScore: score });
  });

  it.each([-1, 11, 2.5, Number.NaN, Number.POSITIVE_INFINITY, "3", null])(
    "refuses the score %s, on either scale",
    (score) => {
      expect(validateFeedback({ ...valid, nauseaScore: score })).toBeNull();
      expect(validateFeedback({ ...valid, dizzinessScore: score })).toBeNull();
    },
  );

  it.each(FEEDBACK_COMFORTS)("accepts the comfort %s", (comfort) => {
    expect(validateFeedback({ ...valid, comfort })?.comfort).toBe(comfort);
  });

  it.each(["great", "", "Good", 1, null])(
    "refuses the comfort %s",
    (comfort) => {
      expect(validateFeedback({ ...valid, comfort })).toBeNull();
    },
  );

  it("accepts 500 characters of text and refuses 501", () => {
    const text = "é".repeat(500);
    expect(validateFeedback({ ...valid, freeText: text })).toEqual({
      ...valid,
      freeText: text,
    });
    expect(validateFeedback({ ...valid, freeText: `${text}x` })).toBeNull();
  });

  it("trims the text before it is measured and stored", () => {
    const text = "x".repeat(500);
    expect(
      validateFeedback({ ...valid, freeText: `  \n${text}\t ` })?.freeText,
    ).toBe(text);
  });

  it.each(["", "   ", "\n\t "])(
    "stores an empty or blank text as no text: %j",
    (freeText) => {
      const feedback = validateFeedback({ ...valid, freeText });
      expect(feedback).toEqual(valid);
      expect(feedback).not.toHaveProperty("freeText");
    },
  );

  it.each([
    ["a missing score", { dizzinessScore: 1, comfort: "good" }],
    ["a missing comfort", { nauseaScore: 1, dizzinessScore: 1 }],
    ["an unknown key", { ...valid, sessionId: "x" }],
    ["a text that is not a string", { ...valid, freeText: 12 }],
    ["a null text", { ...valid, freeText: null }],
    ["an array", [valid]],
    ["null", null],
  ])("refuses %s", (_name, input) => {
    expect(validateFeedback(input)).toBeNull();
  });
});

describe("ANH-146 the console's detail is strict JSON with closed keys", () => {
  it("reads the feedback of a well-formed detail", () => {
    expect(
      parseMachineFeedback(JSON.stringify({ ...valid, freeText: " ok " })),
    ).toEqual({ ...valid, freeText: "ok" });
  });

  it.each([
    ["text that is not JSON", "nauseaScore=3"],
    ["a truncated object", '{"nauseaScore": 3,'],
    ["a JSON string", JSON.stringify(JSON.stringify(valid))],
    ["an unknown key", JSON.stringify({ ...valid, extra: true })],
    ["a prototype key", `{"nauseaScore":3,"dizzinessScore":4,"comfort":"good","__proto__":{}}`],
    ["a score off the scale", JSON.stringify({ ...valid, nauseaScore: 11 })],
    ["an empty detail", ""],
  ])("refuses %s", (_name, detail) => {
    expect(parseMachineFeedback(detail)).toBeNull();
  });
});

describe("ANH-146 the module reads contracts/feedback.json, and only a sound one", () => {
  afterEach(() => {
    vi.doUnmock("../contracts/feedback.json");
    vi.resetModules();
  });

  it("follows a substituted file: it holds no copy of the values", async () => {
    vi.resetModules();
    vi.doMock("../contracts/feedback.json", () => ({
      default: { ...structuredClone(shared), freeTextMaxLength: 120, editWindowHours: 48 },
    }));
    const substituted = await import("./lib/feedback");
    expect(substituted.FREE_TEXT_MAX_LENGTH).toBe(120);
    expect(substituted.EDIT_WINDOW_MS).toBe(48 * 60 * 60 * 1000);
    expect(
      substituted.validateFeedback({ ...valid, freeText: "x".repeat(121) }),
    ).toBeNull();
  });

  it("refuses to load from a file that is not of the shape", async () => {
    vi.resetModules();
    vi.doMock("../contracts/feedback.json", () => ({
      default: { ...structuredClone(shared), editWindowHours: 0 },
    }));
    await expect(import("./lib/feedback")).rejects.toThrow(
      "Invalid feedback contract",
    );
  });
});
