import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  CONSENT_TEXT,
  CONSENT_TEXT_PROVISIONAL,
  CONSENT_TEXT_VERSION,
  consentParagraphs,
} from "./consentText";

const consentMarkdown = readFileSync(resolve(process.cwd(), "docs/consentement.md"), "utf8");
const startMarker = "<!-- texte-du-consentement:debut -->";
const endMarker = "<!-- texte-du-consentement:fin -->";

function markedConsentText(markdown: string): string {
  const start = markdown.indexOf(startMarker);
  const end = markdown.indexOf(endMarker);
  return markdown.slice(start + startMarker.length, end).trim();
}

describe("versioned passenger consent text", () => {
  it("matches the document version and exact marked text", () => {
    expect(consentMarkdown).toContain(`Version du texte : ${CONSENT_TEXT_VERSION}`);
    expect(markedConsentText(consentMarkdown)).toBe(CONSENT_TEXT);
    expect(consentMarkdown.split(startMarker)).toHaveLength(2);
    expect(consentMarkdown.split(endMarker)).toHaveLength(2);
    expect(CONSENT_TEXT_PROVISIONAL).toBe(true);
    expect(consentMarkdown).toContain("TEXTE PROVISOIRE, NON VALIDE juridiquement");
    expect(consentMarkdown).toContain("ANH-87b");
    expect(consentMarkdown).toContain("ANH-172");
    expect(consentMarkdown).toContain("tant que les paramètres médicaux ne sont pas signés (ANH-98)");
  });

  it("renders plain text without Markdown syntax or dash punctuation", () => {
    expect(CONSENT_TEXT).not.toMatch(/(?:^|\n)\s{0,3}(?:#{1,6}\s|[-*+]\s|\d+\.\s|>\s)/m);
    expect(CONSENT_TEXT).not.toMatch(/\[[^\]]+\]\([^)]+\)|(?:\*\*|__|`)/);
    expect(CONSENT_TEXT).not.toContain(String.fromCharCode(0x2014));
    expect(CONSENT_TEXT).not.toContain(String.fromCharCode(0x2013));
  });

  it("splits text into nonempty paragraphs", () => {
    expect(consentParagraphs()).toEqual(CONSENT_TEXT.split("\n\n"));
    expect(consentParagraphs("  first\n\n\n second \n\n")).toEqual(["first", "second"]);
    expect(consentParagraphs("\n\n")).toEqual([]);
  });
});
