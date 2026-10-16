import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

const registry = readFileSync(new URL("men-linear-issues.tsv", import.meta.url), "utf8");
const issue = "[ANH-121](https://linear.app/anheart/issue/ANH-121/cle-machine-reversible-et-machine-supprimee-encore-authentifiee)";
const open = `## MEN-01\n- Status: OPEN\n- Issues: ${issue}\n`;
const mitigated = open.replace("OPEN", "MITIGATED") + "- Evidence: [proof](proof.md)\n";
const accepted = mitigated.replace("MITIGATED", "ACCEPTED");
const url180 = "https://linear.app/anheart/issue/ANH-180/convex-valider-le-passager-dune-seance-locale-et-neutraliser-un";
const url186 = "https://linear.app/anheart/issue/ANH-186/multi-organisation-lot-2-webhook-clerk-vers-le-miroir-convex-signature";
const spellingMessage = /ticket written other than ANH-n:/;
const labelMessage = /Linear link not written \[ANH-n\]\(its registry address\):/;
const addressMessage = /unverified Linear issue link:/;
const referenceMessage = /reference definition or footnote in the catalogue: write inline links/;
const alphabetMessage = /character outside the catalogue alphabet: U\+[0-9A-F]+ at line [0-9]+/;
const otherAddressMessage = /Linear address other than a registered ticket:/;
const destinationMessage = /link destination not written as one token closed by a parenthesis:/;
const scenarios = [
  { name: "refuses missing catalogue", files: {}, status: 1 },
  { name: "resolves open threat to verified existing issue", files: { "menaces.md": open, "rule.md": "MEN-01\n" }, status: 0 },
  { name: "refuses duplicate definition", files: { "menaces.md": open + open }, status: 1 },
  { name: "refuses undefined reference", files: { "menaces.md": open, "rule.md": "MEN-02\n" }, status: 1 },
  { name: "refuses empty catalogue", files: { "menaces.md": "# Catalogue\n" }, status: 1 },
  { name: "refuses open threat without issue", files: { "menaces.md": "## MEN-01\n- Status: OPEN\n" }, status: 1 },
  { name: "refuses nonexistent Linear issue with valid format", files: { "menaces.md": open.replaceAll("ANH-121", "ANH-999999") }, status: 1 },
  { name: "refuses known issue linked to another issue", files: { "menaces.md": open.replace("/ANH-121/", "/ANH-165/") }, status: 1 },
  { name: "refuses known issue linked to foreign host", files: { "menaces.md": open.replace("linear.app", "example.org") }, status: 1 },
  { name: "refuses status omission", files: { "menaces.md": open.replace("- Status: OPEN\n", "") }, status: 1 },
  { name: "refuses unknown status", files: { "menaces.md": open.replace("OPEN", "OPNE") }, status: 1 },
  { name: "refuses unsigned acceptance", files: { "menaces.md": open.replace("OPEN", "ACCEPTED") }, status: 1 },
  { name: "refuses mitigation without evidence", files: { "menaces.md": open.replace("OPEN", "MITIGATED") }, status: 1 },
  { name: "refuses duplicate status", files: { "menaces.md": open + "- Status: MITIGATED\n" }, status: 1 },
  { name: "refuses duplicate issue fields", files: { "menaces.md": open + `- Issues: ${issue}\n` }, status: 1 },
  { name: "refuses one uncovered threat among covered threats", files: { "menaces.md": open + "## MEN-02\n- Status: OPEN\n" }, status: 1 },
  { name: "refuses malformed identifier", files: { "menaces.md": open.replace("MEN-01", "MEN-1") }, status: 1 },
  { name: "refuses malformed registry", files: { "menaces.md": open }, registry: "ANH-121\n", status: 1 },
  { name: "refuses duplicate registry issue", files: { "menaces.md": open }, registry: registry + registry, status: 1 },
  { name: "refuses one unverified issue beside a verified issue", files: { "menaces.md": open.trimEnd() + ", [ANH-999999](https://linear.app/anheart/issue/ANH-999999/unknown)\n" }, status: 1 },
  { name: "resolves multiple covered threats", files: { "menaces.md": open + open.replace("MEN-01", "MEN-02") }, status: 0 },
  { name: "accepts documented mitigation with evidence", files: { "menaces.md": mitigated, "proof.md": "Synthetic verification record\n" }, status: 0 },
  { name: "refuses empty mitigation evidence", files: { "menaces.md": mitigated, "proof.md": "" }, status: 1 },
  { name: "refuses missing mitigation evidence file", files: { "menaces.md": mitigated }, status: 1 },
  { name: "refuses evidence escaping documentation", files: { "menaces.md": mitigated.replace("(proof.md)", "(../proof.md)") }, status: 1 },
  { name: "refuses acceptance without named signer", files: { "menaces.md": accepted + "- Reviewed-on: 2026-10-05\n", "proof.md": "Synthetic decision fixture\n" }, status: 1 },
  { name: "refuses acceptance without review date", files: { "menaces.md": accepted + "- Signed-by: Synthetic fixture signer\n", "proof.md": "Synthetic decision fixture\n" }, status: 1 },
  { name: "accepts acceptance metadata with evidence for human review", files: { "menaces.md": accepted + "- Signed-by: Synthetic fixture signer\n- Reviewed-on: 2026-10-05\n", "proof.md": "Synthetic decision fixture, not a human risk acceptance\n" }, status: 0 },
  // ANH-221: a ticket named in the text, outside any Issues field, once escaped the registry.
  { name: "accepts a ticket named in the text that the registry carries", files: { "menaces.md": open + "\nVoir aussi ANH-165.\n" }, status: 0 },
  { name: "refuses a ticket named in the text that the registry does not carry", files: { "menaces.md": open + "\nVoir aussi ANH-999999.\n" }, status: 1 },
  { name: "refuses a ticket named in a heading that the registry does not carry", files: { "menaces.md": "# Menaces (ANH-999999)\n\n" + open }, status: 1 },
  { name: "accepts a Linear link in the text that matches the registry", files: { "menaces.md": open + "\nVoir [ANH-165](https://linear.app/anheart/issue/ANH-165/convex-rotation-revocation-et-expiration-des-cles-machine-avec).\n" }, status: 0 },
  { name: "refuses a Linear link in the text whose address is not the registry's", files: { "menaces.md": open + "\nVoir [ANH-165](https://linear.app/anheart/issue/ANH-165/convex-rotation).\n" }, status: 1 },
  { name: "refuses a Linear link in the text whose label names another ticket", files: { "menaces.md": open + `\nVoir ${issue.replace("[ANH-121]", "[ANH-165]")}.\n` }, status: 1 },
  { name: "refuses a Linear link in the text to a ticket the registry does not carry", files: { "menaces.md": open + "\nVoir [la décision](https://linear.app/anheart/issue/ANH-999999/unknown).\n" }, status: 1 },
  // ANH-229 EX-2 : alternate ticket spellings must not escape the registry boundary.
  { name: "EX-2 refuses an unknown ticket with a space", files: { "menaces.md": open + "Voir ANH 999999.\n" }, status: 1, message: spellingMessage },
  { name: "EX-2 refuses a lowercase unknown ticket", files: { "menaces.md": open + "Voir anh-999999.\n" }, status: 1, message: spellingMessage },
  { name: "EX-2 refuses a nonbreaking hyphen", files: { "menaces.md": open + "Voir ANH\u2011999999.\n" }, status: 1, message: spellingMessage },
  { name: "EX-2 refuses a registered ticket with a space", files: { "menaces.md": open + "Voir ANH 165.\n" }, status: 1, message: spellingMessage },
  { name: "EX-2 refuses a letter stuck to a ticket", files: { "menaces.md": open + "Voir ANH-165a.\n" }, status: 1, message: spellingMessage },
  { name: "EX-2 refuses a bold label for another ticket", files: { "menaces.md": open + `[**ANH-180**](${url186})\n` }, status: 1, message: labelMessage },
  { name: "EX-2 refuses a label with extra words for another ticket", files: { "menaces.md": open + `[ANH-180 lot 2](${url186})\n` }, status: 1, message: labelMessage },
  { name: "EX-2 refuses a code label for another ticket", files: { "menaces.md": open + `[\`ANH-180\`](${url186})\n` }, status: 1, message: labelMessage },
  { name: "EX-2 refuses another ticket behind a titled link", files: { "menaces.md": open + `[ANH-180](${url186} "titre")\n` }, status: 1, message: destinationMessage },
  { name: "EX-2 refuses another ticket behind an angled destination", files: { "menaces.md": open + `[ANH-180](<${url186}>)\n` }, status: 1, message: destinationMessage },
  { name: "EX-2 refuses a full reference to another ticket", files: { "menaces.md": open + `[ANH-180][x]\n\n[x]: ${url186}\n` }, status: 1, message: referenceMessage },
  { name: "EX-2 refuses a shortcut reference with a lowercase definition", files: { "menaces.md": open + `[ANH-180]\n\n[anh-180]: ${url186}\n` }, status: 1, message: referenceMessage },
  { name: "EX-2 refuses a lowercase Linear issue address", files: { "menaces.md": open + "[la décision](https://linear.app/anheart/issue/anh-999999/x)\n" }, status: 1, message: /unverified Linear issue link: anh-999999/ },
  { name: "EX-2 refuses an HTML link", files: { "menaces.md": open + `<a href="${url186}">ANH-180</a>\n` }, status: 1, message: /HTML in the catalogue: write Markdown/ },
  // ANH-229 EX-3 : exact addresses survive prose punctuation, code and comment fragments.
  { name: "EX-3 accepts an exact bare address followed by a full stop", files: { "menaces.md": open + `${url180}.\n` }, status: 0 },
  { name: "EX-3 accepts an exact address in code", files: { "menaces.md": open + `\`${url180}\`\n` }, status: 0 },
  { name: "EX-3 accepts a labelled link to an exact address with a comment", files: { "menaces.md": open + `[ANH-180](${url180}#comment-1)\n` }, status: 0 },
  // ANH-229 EX-4 : free labels isolate address validation from label validation.
  { name: "EX-4 refuses a truncated registered address with a free label", files: { "menaces.md": open + "[la décision](https://linear.app/anheart/issue/ANH-165/convex-rotation)\n" }, status: 1, message: /unverified Linear issue link: ANH-165/ },
  { name: "EX-4 refuses an extended address with a free label", files: { "menaces.md": open + `[la décision](${url180}_extra)\n` }, status: 1, message: addressMessage },
  { name: "EX-4 refuses an HTTP address with a free label", files: { "menaces.md": open + `[la décision](${url180.replace("https:", "http:")})\n` }, status: 1, message: addressMessage },
  // ANH-229 EX-2 : valid Markdown and lowercase path fragments remain usable.
  { name: "accepts a correctly labelled registered link", files: { "menaces.md": open + `[ANH-180](${url180})\n` }, status: 0 },
  { name: "refuses a correctly labelled registered reference", files: { "menaces.md": open + `[ANH-180][r]\n\n[r]: ${url180}\n` }, status: 1, message: referenceMessage },
  { name: "refuses a free label to a registered address", files: { "menaces.md": open + `[la décision](${url180})\n` }, status: 1, message: labelMessage },
  { name: "accepts a lowercase review filename", files: { "menaces.md": open + "[x](reviews/anh-136-2026-10-05.md)\n" }, status: 0 },
  { name: "accepts a lowercase release anchor", files: { "menaces.md": open + "[y](framework-de-test.md#pipeline-de-release-anh-219)\n" }, status: 0 },
  { name: "accepts a lowercase path in code", files: { "menaces.md": open + "`/root/anh136_threat_model`\n" }, status: 0 },
  { name: "refuses a Linear review with a free label", files: { "menaces.md": open + "[la PR](https://linear.app/anheart/review/x)\n" }, status: 1, message: otherAddressMessage },
  // ANH-229 EX-2 : Unicode separators, titles and images follow the same rules.
  { name: "EX-2 refuses an invisible separator with a readable diagnostic", files: { "menaces.md": open + "Voir ANH\u200b165.\n" }, status: 1, message: /ticket written other than ANH-n: "ANH\\u200b165"/ },
  { name: "EX-2 refuses a soft hyphen with a readable diagnostic", files: { "menaces.md": open + "Voir ANH\u00ad165.\n" }, status: 1, message: /ticket written other than ANH-n: "ANH\\u00ad165"/ },
  { name: "EX-2 refuses a Unicode minus", files: { "menaces.md": open + "Voir ANH\u2212165.\n" }, status: 1, message: spellingMessage },
  { name: "EX-2 refuses a Unicode space", files: { "menaces.md": open + "Voir ANH\u2002165.\n" }, status: 1, message: spellingMessage },
  { name: "EX-2 refuses mixed case", files: { "menaces.md": open + "Voir AnH-165.\n" }, status: 1, message: spellingMessage },
  { name: "EX-2 refuses a ticket suffix with an underscore", files: { "menaces.md": open + "Voir ANH-165_extra.\n" }, status: 1, message: spellingMessage },
  { name: "EX-2 refuses a local destination with a ticket in its title", files: { "menaces.md": open + '[x](reviews/anh-136.md "ANH 165")\n' }, status: 1, message: destinationMessage },
  { name: "EX-2 refuses an image labelled with another ticket", files: { "menaces.md": open + `![**ANH-180**](${url186})\n` }, status: 1, message: labelMessage },
  { name: "EX-2 refuses a collapsed reference to another ticket", files: { "menaces.md": open + `[ANH-180][]\n\n[ANH-180]: ${url186}\n` }, status: 1, message: referenceMessage },
  { name: "EX-2 refuses a shortcut reference to another ticket", files: { "menaces.md": open + `[ANH-180]\n\n[ANH-180]: ${url186}\n` }, status: 1, message: referenceMessage },
  { name: "EX-2 refuses a reference with mixed case and whitespace", files: { "menaces.md": open + `[ANH-180][X   Y]\n\n[x y]: <${url186}> "titre"\n` }, status: 1, message: referenceMessage },
  // ANH-229 EX-4 : all address forms retain exact registry comparison.
  { name: "EX-4 refuses a WWW issue address", files: { "menaces.md": open + `[la décision](${url180.replace("https://", "www.")})\n` }, status: 1, message: addressMessage },
  { name: "EX-4 refuses a Linear subdomain", files: { "menaces.md": open + `<${url180.replace("linear.app", "x.linear.app")}>\n` }, status: 1, message: addressMessage },
  { name: "EX-4 refuses an uppercase Linear host", files: { "menaces.md": open + `${url180.replace("linear.app", "LINEAR.APP")}\n` }, status: 1, message: addressMessage },
  { name: "EX-4 refuses another workspace", files: { "menaces.md": open + `[la décision](${url180.replace("/anheart/", "/other/")})\n` }, status: 1, message: addressMessage },
  { name: "EX-4 refuses an address query", files: { "menaces.md": open + `[la décision](${url180}?x=1)\n` }, status: 1, message: addressMessage },
  { name: "EX-4 checks registered names even inside a foreign address", files: { "menaces.md": open + "[x](https://example.org/ANH-999999/x)\n" }, status: 1, message: /Linear issue named but not verified: ANH-999999/ },
  // ANH-229 EX-2 : destinations are excluded from spelling checks; the alphabet is literal.
  { name: "accepts lowercase ticket names inside non-Linear destinations", files: { "menaces.md": open + "[x](https://example.org/anh-999999/x)\n" }, status: 0 },
  { name: "refuses an NFKC spelling of a registered name", files: { "menaces.md": open + "Voir ＡＮＨ-１６５.\n" }, status: 1, message: alphabetMessage },
  { name: "refuses exact reference destinations with fragments and titles", files: { "menaces.md": open + `[ANH-180][r]\n\n[r]: <${url180}#comment-1> "titre"\n` }, status: 1, message: referenceMessage },
  // ANH-229 EX-4 : explicit destinations retain punctuation and are classified by host.
  { name: "EX-4 refuses a full stop in an explicitly labelled destination", files: { "menaces.md": open + `[ANH-180](${url180}.)\n` }, status: 1, message: addressMessage },
  { name: "EX-4 refuses a full stop in an explicit free-label destination", files: { "menaces.md": open + `[la décision](${url180}.)\n` }, status: 1, message: addressMessage },
  { name: "EX-4 refuses a full stop in an angled destination", files: { "menaces.md": open + `[la décision](<${url180}.>)\n` }, status: 1, message: destinationMessage },
  { name: "EX-4 refuses a full stop in an autolink", files: { "menaces.md": open + `<${url180}.>\n` }, status: 1, message: addressMessage },
  { name: "EX-4 refuses a full stop in a reference destination", files: { "menaces.md": open + `[la décision][r]\n\n[r]: ${url180}.\n` }, status: 1, message: referenceMessage },
  { name: "EX-4 refuses an apostrophe in a Linear destination", files: { "menaces.md": open + `[ANH-180](${url186}')\n` }, status: 1, message: addressMessage },
  { name: "EX-4 refuses an asterisk in a Linear destination", files: { "menaces.md": open + `[ANH-180](${url186}*)\n` }, status: 1, message: addressMessage },
  { name: "EX-4 refuses user information in a Linear destination", files: { "menaces.md": open + `[ANH-180](${url186.replace("https://", "https://x@")})\n` }, status: 1, message: addressMessage },
  { name: "EX-4 refuses a truncated Linear destination with user information", files: { "menaces.md": open + "[la décision](https://x@linear.app/anheart/issue/ANH-165/convex-rotation)\n" }, status: 1, message: addressMessage },
  { name: "EX-4 refuses an unreadable destination containing the Linear host", files: { "menaces.md": open + `[la décision](${url180.replace("linear.app", "[linear.app")})\n` }, status: 1, message: addressMessage },
  // ANH-229 EX-2 : reference definitions are refused wherever they appear.
  { name: "EX-2 refuses a reference destination on the following line", files: { "menaces.md": open + `[ANH-180][x]\n\n[x]:\n${url186}\n` }, status: 1, message: referenceMessage },
  { name: "EX-2 refuses a reference definition in a quotation", files: { "menaces.md": open + `[ANH-180][x]\n\n> [x]: ${url186}\n` }, status: 1, message: referenceMessage },
  { name: "EX-2 refuses a reference definition in a list", files: { "menaces.md": open + `[ANH-180][x]\n\n- [x]: ${url186}\n` }, status: 1, message: referenceMessage },
  { name: "EX-2 refuses a multiline reference label", files: { "menaces.md": open + `[ANH-180][x y]\n\n[x\ny]: ${url186}\n` }, status: 1, message: referenceMessage },
  { name: "EX-2 refuses a real definition hidden behind a false one", files: { "menaces.md": open + `[ANH-180][x]\n\nTexte [x]: ${url180}\n\n[x]: ${url186}\n` }, status: 1, message: referenceMessage },
  { name: "refuses a canonical reference destination on the following line", files: { "menaces.md": open + `[ANH-180][r]\n\n[r]:\n${url180}\n` }, status: 1, message: referenceMessage },
  // ANH-229 EX-2 : escapes and emphasis cannot hide the separator of a ticket name.
  { name: "EX-2 refuses an escaped ticket separator", files: { "menaces.md": open + "Voir ANH\\-999.\n" }, status: 1, message: spellingMessage },
  { name: "EX-2 refuses bold markup inside a ticket name", files: { "menaces.md": open + "Voir **ANH**-999.\n" }, status: 1, message: spellingMessage },
  { name: "EX-2 refuses code markup inside a ticket name", files: { "menaces.md": open + "Voir `ANH`-999.\n" }, status: 1, message: spellingMessage },
  { name: "EX-2 refuses strikethrough markup inside a ticket name", files: { "menaces.md": open + "Voir ~~ANH~~-999.\n" }, status: 1, message: spellingMessage },
  { name: "EX-2 refuses an escaped label for another ticket", files: { "menaces.md": open + `[ANH\\-180](${url186})\n` }, status: 1, message: spellingMessage },
  // ANH-229 EX-2 : character references cannot hide ticket letters or separators.
  { name: "EX-2 refuses a decimal character reference in a ticket separator", files: { "menaces.md": open + "Voir ANH&#45;999.\n" }, status: 1, message: /character reference in the catalogue: write the character itself: "&#45;"/ },
  { name: "EX-2 refuses a character reference in a label for another ticket", files: { "menaces.md": open + `[ANH&#45;180](${url186})\n` }, status: 1, message: /character reference in the catalogue: write the character itself: "&#45;"/ },
  { name: "EX-2 refuses a decimal character reference in a ticket letter", files: { "menaces.md": open + "Voir &#65;NH-999.\n" }, status: 1, message: /character reference in the catalogue: write the character itself: "&#65;"/ },
  { name: "EX-2 refuses a named character reference", files: { "menaces.md": open + "Voir ANH&hyphen;999.\n" }, status: 1, message: /character reference in the catalogue: write the character itself: "&hyphen;"/ },
  { name: "EX-2 refuses a lowercase hexadecimal character reference", files: { "menaces.md": open + "Voir ANH&#x2d;999.\n" }, status: 1, message: /character reference in the catalogue: write the character itself: "&#x2d;"/ },
  { name: "EX-2 refuses an uppercase hexadecimal character reference", files: { "menaces.md": open + "Voir ANH&#X2D;999.\n" }, status: 1, message: /character reference in the catalogue: write the character itself: "&#X2D;"/ },
  // ANH-229 EX-2 : HTML is refused throughout the page, including code spans.
  { name: "EX-2 refuses an HTML span inside a ticket name", files: { "menaces.md": open + "Voir ANH<span>-999</span>.\n" }, status: 1, message: /HTML in the catalogue: write Markdown/ },
  { name: "EX-2 refuses an HTML comment", files: { "menaces.md": open + "Voir <!-- x -->.\n" }, status: 1, message: /HTML in the catalogue: write Markdown/ },
  { name: "EX-2 refuses an HTML line break", files: { "menaces.md": open + "Voir <br>.\n" }, status: 1, message: /HTML in the catalogue: write Markdown/ },
  { name: "EX-2 refuses HTML in a code span", files: { "menaces.md": open + "Voir `<span>`.\n" }, status: 1, message: /HTML in the catalogue: write Markdown/ },
  { name: "accepts an HTTPS autolink to a non-Linear address", files: { "menaces.md": open + "<https://example.org/x>\n" }, status: 0 },
  { name: "accepts an HTTPS autolink to an exact registered address", files: { "menaces.md": open + `<${url180}>\n` }, status: 0 },
  // ANH-229 EX-2 : R1 definitions and footnotes cannot erase visible ticket text.
  { name: "R1 refuses a false definition inside prose", files: { "menaces.md": open + "Voir [x]: ANH 999.\n" }, status: 1, message: referenceMessage },
  { name: "R1 refuses a lowercase ticket after a false definition", files: { "menaces.md": open + "Voir [note]: anh-999.\n" }, status: 1, message: referenceMessage },
  { name: "R1 refuses a false definition before a line break", files: { "menaces.md": open + "[x]:\nANH 999\n" }, status: 1, message: referenceMessage },
  { name: "R1 refuses a footnote containing an alternate ticket spelling", files: { "menaces.md": open + "[^n]: ANH 999\n" }, status: 1, message: referenceMessage },
  { name: "R1 refuses a shortcut followed by a colon in prose", files: { "menaces.md": open + `Voir [ANH-180]: détails.\n\n[ANH-180]: ${url186}\n` }, status: 1, message: referenceMessage },
  { name: "R1 refuses an escaped reference label", files: { "menaces.md": open + `[ANH-180][x\\]]\n\n[x\\]]: ${url186}\n` }, status: 1, message: referenceMessage },
  { name: "R1 refuses a Unicode case-folded reference", files: { "menaces.md": open + `[ANH-180][Straße]\n\n[STRASSE]: ${url186}\n` }, status: 1, message: referenceMessage },
  { name: "R1 refuses a mismatched first reference definition", files: { "menaces.md": open + `[ANH-180][x]\n\n[x]: ${url186}\n[x]: ${url180}\n` }, status: 1, message: referenceMessage },
  { name: "refuses a reference marker even in code", files: { "menaces.md": open + "`]:`\n" }, status: 1, message: referenceMessage },
  // ANH-229 EX-2 : R1 split letters and digits are candidates, rather than hidden names.
  { name: "R1 refuses a partially bold label", files: { "menaces.md": open + `[A**NH**-180](${url186})\n` }, status: 1, message: spellingMessage },
  { name: "R1 refuses a partially italic label", files: { "menaces.md": open + `[*A*NH-180](${url186})\n` }, status: 1, message: spellingMessage },
  { name: "R1 refuses partially bold ticket letters in prose", files: { "menaces.md": open + "Voir A**NH**-999.\n" }, status: 1, message: spellingMessage },
  { name: "R1 refuses partially italic ticket letters in prose", files: { "menaces.md": open + "*A*NH-999\n" }, status: 1, message: spellingMessage },
  { name: "R1 refuses code markup between ticket letters", files: { "menaces.md": open + "AN`H`-999\n" }, status: 1, message: spellingMessage },
  { name: "R1 refuses markup between ticket digits", files: { "menaces.md": open + "Voir ANH-165**0**.\n" }, status: 1, message: spellingMessage },
  // ANH-229 EX-2 : R1 labels must be literal even when Markdown hides their spelling.
  { name: "R1 refuses an invisible character inside a label", files: { "menaces.md": open + `[A\u200bNH-180](${url186})\n` }, status: 1, message: labelMessage },
  { name: "R1 refuses Greek letters inside a label", files: { "menaces.md": open + `[ΑΝΗ-180](${url186})\n` }, status: 1, message: labelMessage },
  { name: "R1 refuses a code closing bracket after a ticket label", files: { "menaces.md": open + `[ANH-180\`]\`](${url186})\n` }, status: 1, message: labelMessage },
  { name: "R1 refuses a code closing bracket before a ticket label", files: { "menaces.md": open + `[\`]\`ANH-180](${url186})\n` }, status: 1, message: labelMessage },
  { name: "R1 refuses a mismatched label with an escaped double-quoted title", files: { "menaces.md": open + `[ANH-180](${url186} "a\\"b")\n` }, status: 1, message: destinationMessage },
  { name: "R1 refuses a mismatched label with an escaped parenthesized title", files: { "menaces.md": open + `[ANH-180](${url186} (a\\)b))\n` }, status: 1, message: destinationMessage },
  { name: "R1 refuses a mismatched label with an escaped single-quoted title", files: { "menaces.md": open + `[ANH-180](${url186} 'a\\'b')\n` }, status: 1, message: destinationMessage },
  { name: "refuses a formatted label even for its own registered ticket", files: { "menaces.md": open + `[**ANH-180**](${url180})\n` }, status: 1, message: labelMessage },
  { name: "refuses an escaped closing bracket in a label", files: { "menaces.md": open + `[ANH-180\\]](${url186})\n` }, status: 1, message: labelMessage },
  // ANH-229 EX-2 : R1 uses the raw alphabet, without Unicode normalization.
  { name: "R1 refuses a zero-width space between ticket letters", files: { "menaces.md": open + "Voir A\u200bNH-999.\n" }, status: 1, message: /character outside the catalogue alphabet: U\+200B at line 4/ },
  { name: "R1 refuses a soft hyphen between ticket letters", files: { "menaces.md": open + "AN\u00adH-999\n" }, status: 1, message: /character outside the catalogue alphabet: U\+00AD at line 4/ },
  { name: "R1 refuses a word joiner between ticket letters", files: { "menaces.md": open + "A\u2060NH-999\n" }, status: 1, message: alphabetMessage },
  { name: "R1 refuses a combining grapheme joiner", files: { "menaces.md": open + "A\u034fNH-999\n" }, status: 1, message: alphabetMessage },
  { name: "R1 refuses a variation selector between ticket letters", files: { "menaces.md": open + "A\ufe0fNH-999\n" }, status: 1, message: alphabetMessage },
  { name: "R1 refuses an invisible separator between ticket digits", files: { "menaces.md": open + "Voir ANH-165\u200b0.\n" }, status: 1, message: alphabetMessage },
  { name: "R1 refuses Cyrillic ticket homoglyphs", files: { "menaces.md": open + "Voir АNН-999.\n" }, status: 1, message: alphabetMessage },
  { name: "R1 refuses Greek ticket homoglyphs in prose", files: { "menaces.md": open + "ΑΝΗ-999\n" }, status: 1, message: alphabetMessage },
  { name: "R1 refuses a box-drawing separator", files: { "menaces.md": open + "ANH─999\n" }, status: 1, message: alphabetMessage },
  { name: "R1 refuses a prolonged sound mark separator", files: { "menaces.md": open + "ANHー999\n" }, status: 1, message: alphabetMessage },
  { name: "R1 refuses a hyphen bullet separator", files: { "menaces.md": open + "ANH⁃999\n" }, status: 1, message: alphabetMessage },
  { name: "R1 refuses a modifier minus separator", files: { "menaces.md": open + "ANH˗999\n" }, status: 1, message: alphabetMessage },
  { name: "R1 refuses a heavy minus separator", files: { "menaces.md": open + "ANH➖999\n" }, status: 1, message: alphabetMessage },
  { name: "R1 refuses a fullwidth unknown ticket", files: { "menaces.md": open + "Voir ＡＮＨ-９９９.\n" }, status: 1, message: alphabetMessage },
  { name: "R1 refuses a fullwidth label for another ticket", files: { "menaces.md": open + `[ＡＮＨ-180](${url186})\n` }, status: 1, message: labelMessage },
  { name: "R1 refuses a tab in prose", files: { "menaces.md": open + "Voir\tici.\n" }, status: 1, message: /character outside the catalogue alphabet: U\+0009 at line 4/ },
  { name: "refuses multiplication outside the alphabet", files: { "menaces.md": open + "x × y\n" }, status: 1, message: alphabetMessage },
  { name: "refuses division outside the alphabet", files: { "menaces.md": open + "x ÷ y\n" }, status: 1, message: alphabetMessage },
  { name: "refuses a supplementary code point with its full diagnostic", files: { "menaces.md": open + "\u{1f600}\n" }, status: 1, message: /character outside the catalogue alphabet: U\+1F600 at line 4/ },
  // ANH-229 EX-4 : R1 host spelling and every Linear path keep exact registry comparison.
  { name: "R1 refuses a dotted Linear host in a labelled link", files: { "menaces.md": open + `[ANH-180](${url186.replace("linear.app", "linear.app.")})\n` }, status: 1, message: addressMessage },
  { name: "R1 refuses a truncated dotted Linear host in a free-label link", files: { "menaces.md": open + "[la décision](https://linear.app./anheart/issue/ANH-165/convex-rotation)\n" }, status: 1, message: addressMessage },
  { name: "R1 refuses a truncated dotted Linear host in a bare address", files: { "menaces.md": open + "https://linear.app./anheart/issue/ANH-165/convex-rotation\n" }, status: 1, message: addressMessage },
  { name: "R1 refuses doubled path slashes in a Linear issue address", files: { "menaces.md": open + "https://linear.app//anheart/issue/ANH-165/convex-rotation\n" }, status: 1, message: addressMessage },
  { name: "R1 refuses an encoded slash before the issue path", files: { "menaces.md": open + "https://linear.app/anheart%2Fissue/ANH-165/convex-rotation\n" }, status: 1, message: addressMessage },
  { name: "R1 refuses an encoded slash after a mixed-case issue path", files: { "menaces.md": open + "https://linear.app/anheart/IsSuE%2FANH-165/convex-rotation\n" }, status: 1, message: addressMessage },
  { name: "R1 refuses a bare Linear review address", files: { "menaces.md": open + "https://linear.app/anheart/review/x\n" }, status: 1, message: otherAddressMessage },
  { name: "R1 refuses a Linear project address", files: { "menaces.md": open + "https://linear.app/anheart/project/x\n" }, status: 1, message: otherAddressMessage },
  { name: "R1 refuses a Linear document address", files: { "menaces.md": open + "https://linear.app/anheart/document/x\n" }, status: 1, message: otherAddressMessage },
  // ANH-229 EX-2 : erasure applies only to destination tokens containing address punctuation.
  { name: "accepts a short lowercase destination filename", files: { "menaces.md": open + "[x](anh-136.md)\n" }, status: 0 },
  { name: "accepts a short lowercase destination filename with an anchor", files: { "menaces.md": open + "[x](anh-136.md#y)\n" }, status: 0 },
  { name: "refuses an address-free token after a link opener", files: { "menaces.md": open + "](ANH 999\n" }, status: 1, message: destinationMessage },
  { name: "refuses an alternate ticket used as a destination token", files: { "menaces.md": open + "[x](anh-165)\n" }, status: 1, message: spellingMessage },
  // ANH-229 EX-2 : R1c destination tokens cannot hide visible lowercase tickets.
  { name: "R1c refuses a lowercase ticket after a false link opener", files: { "menaces.md": open + "Voir ](anh-180.) ici.\n" }, status: 1, message: spellingMessage },
  { name: "R1c refuses a lowercase ticket after an escaped closing bracket", files: { "menaces.md": open + "Voir [x\\](anh-999.) ici.\n" }, status: 1, message: spellingMessage },
  { name: "R1c refuses a lowercase destination token in code", files: { "menaces.md": open + "Voir `](anh-999.)` ici.\n" }, status: 1, message: spellingMessage },
  // ANH-229 EX-2 : the strict subset retains the page's actual link and character forms.
  { name: "refuses a canonical link with a fragment and title", files: { "menaces.md": open + `[ANH-180](${url180}#comment-1 "voir")\n` }, status: 1, message: destinationMessage },
  { name: "refuses an angled canonical inline destination", files: { "menaces.md": open + `[ANH-180](<${url180}>)\n` }, status: 1, message: destinationMessage },
  { name: "accepts a canonical image destination", files: { "menaces.md": open + `![ANH-180](${url180})\n` }, status: 0 },
  { name: "refuses a canonical inline destination on the next line", files: { "menaces.md": open + `[ANH-180](\n ${url180})\n` }, status: 1, message: destinationMessage },
  { name: "accepts the full catalogue alphabet after CRLF normalization", files: { "menaces.md": open + "« · » À É à â ç è é ê î ô ù û œ → ↔\u00a0\u202f\u2019Œ\u2014\r\n" }, status: 0 },
  { name: "accepts lowercase slash fragments anywhere", files: { "menaces.md": open + "Voir /anh-999.\n" }, status: 0 },
  { name: "accepts lowercase hyphen fragments anywhere", files: { "menaces.md": open + "Voir -anh-999.\n" }, status: 0 },
  // ANH-229 EX-2 and EX-4 : R1b destinations must be a single closed token.
  { name: "R1b refuses parentheses hiding a mismatched Linear host", files: { "menaces.md": open + `[ANH-180](${url186.replace("https://", "https://()@")})\n` }, status: 1, message: destinationMessage },
  { name: "R1b refuses parentheses hiding a truncated Linear address", files: { "menaces.md": open + "[la décision](https://()@linear.app/anheart/issue/ANH-165/convex-rotation)\n" }, status: 1, message: destinationMessage },
  { name: "R1b refuses a chevron hiding a mismatched Linear host", files: { "menaces.md": open + `[ANH-180](${url186.replace("https://", "https://>@")})\n` }, status: 1, message: destinationMessage },
  { name: "R1b refuses an opening parenthesis in user information", files: { "menaces.md": open + `[ANH-180](${url186.replace("https://", "https://(@")}))\n` }, status: 1, message: destinationMessage },
  { name: "R1b refuses an escaped parenthesis in user information", files: { "menaces.md": open + `[ANH-180](${url186.replace("https://", "https://\\(@")})\n` }, status: 1, message: destinationMessage },
  { name: "R1b refuses parentheses before an encoded Linear host", files: { "menaces.md": open + `[ANH-180](${url186.replace("https://linear", "https://()@%6Cinear")})\n` }, status: 1, message: destinationMessage },
  { name: "R1b refuses 300 spaces before a Linear destination", files: { "menaces.md": open + `[ANH-180](${" ".repeat(300)}${url186})\n` }, status: 1, message: destinationMessage },
  { name: "R1b refuses a newline before a mismatched Linear destination", files: { "menaces.md": open + `[ANH-180](\n${url186})\n` }, status: 1, message: destinationMessage },
  { name: "R1b refuses a newline and 300 spaces before a destination", files: { "menaces.md": open + `[ANH-180](\n${" ".repeat(300)}${url186})\n` }, status: 1, message: destinationMessage },
  { name: "R1b refuses spaces before an angled destination", files: { "menaces.md": open + `[ANH-180](${" ".repeat(300)}<${url186}>)\n` }, status: 1, message: destinationMessage },
  { name: "R1b refuses a quoted link on two lines", files: { "menaces.md": open + `> [ANH-180](\n> ${url186})\n` }, status: 1, message: destinationMessage },
  { name: "R1b refuses a quoted list link on two lines", files: { "menaces.md": open + `- > [ANH-180](\n  > ${url186})\n` }, status: 1, message: destinationMessage },
  { name: "refuses an empty inline destination", files: { "menaces.md": open + "[x]()\n" }, status: 1, message: destinationMessage },
  { name: "refuses an unterminated inline destination", files: { "menaces.md": open + "[x](x.md\n" }, status: 1, message: destinationMessage },
  { name: "refuses parentheses after a canonical inline destination", files: { "menaces.md": open + `[ANH-180](${url180}(x))\n` }, status: 1, message: destinationMessage },
  // ANH-229 EX-2 : R1b literal labels cannot begin with an escaped bracket.
  { name: "R1b refuses an escaped opening bracket after another ticket", files: { "menaces.md": open + `[ANH-180 \\[ANH-186](${url186})\n` }, status: 1, message: labelMessage },
  { name: "R1b refuses an escaped opening bracket after a bold ticket", files: { "menaces.md": open + `[**ANH-180** \\[ANH-186](${url186})\n` }, status: 1, message: labelMessage },
  { name: "R1b refuses an escaped opening bracket on the next line", files: { "menaces.md": open + `[ANH-180\n\\[ANH-186](${url186})\n` }, status: 1, message: labelMessage },
  { name: "R1b refuses three escapes before an opening bracket", files: { "menaces.md": open + `[ANH-180 ${"\\".repeat(3)}[ANH-186](${url186})\n` }, status: 1, message: labelMessage },
  // ANH-229 EX-4 : M20 must require the opening bracket of the entire label.
  { name: "R1b refuses two tickets in a label ending with the right ticket", files: { "menaces.md": open + `[ANH-180 ANH-186](${url186})\n` }, status: 1, message: labelMessage },
  { name: "R1b refuses extra words before the right ticket", files: { "menaces.md": open + `[Voir ANH-180](${url180})\n` }, status: 1, message: labelMessage },
  // ANH-229 EX-2 : R1b lowercase emphasis is not a filename fragment.
  { name: "R1b refuses an italic lowercase unknown ticket", files: { "menaces.md": open + "Voir _anh-999_.\n" }, status: 1, message: spellingMessage },
  { name: "R1b refuses a bold lowercase unknown ticket", files: { "menaces.md": open + "__anh-999__\n" }, status: 1, message: spellingMessage },
  { name: "R1b refuses an italic lowercase ticket with a space", files: { "menaces.md": open + "_anh 999_\n" }, status: 1, message: spellingMessage },
  { name: "R1b refuses an italic lowercase registered ticket", files: { "menaces.md": open + "_anh-180_\n" }, status: 1, message: spellingMessage },
  { name: "accepts a lowercase underscore fragment attached to a word", files: { "menaces.md": open + "x_anh-1\n" }, status: 0 },
  { name: "accepts a ticket label pointing to a local page", files: { "menaces.md": open + "[ANH-223](pi-reseau.md)\n" }, status: 0 },
  // ANH-229 EX-2 : R1b punctuation and partial links are ticket separators.
  { name: "R1b refuses a spaced hash separator", files: { "menaces.md": open + "ANH #999\n" }, status: 1, message: spellingMessage },
  { name: "R1b refuses a hyphen and hash separator", files: { "menaces.md": open + "ANH-#999\n" }, status: 1, message: spellingMessage },
  { name: "R1b refuses a full stop separator", files: { "menaces.md": open + "ANH.999\n" }, status: 1, message: spellingMessage },
  { name: "R1b refuses a slash separator", files: { "menaces.md": open + "ANH/999\n" }, status: 1, message: spellingMessage },
  { name: "R1b refuses a colon separator", files: { "menaces.md": open + "ANH:999\n" }, status: 1, message: spellingMessage },
  { name: "R1b refuses a middle dot separator", files: { "menaces.md": open + "ANH·999\n" }, status: 1, message: spellingMessage },
  { name: "R1b refuses linked digits after a hyphen", files: { "menaces.md": open + "ANH-[999](x.md)\n" }, status: 1, message: spellingMessage },
  { name: "R1b refuses a linked hyphen before the digits", files: { "menaces.md": open + "ANH[-](x.md)999\n" }, status: 1, message: spellingMessage },
  { name: "refuses an opening parenthesis separator", files: { "menaces.md": open + "ANH(999\n" }, status: 1, message: spellingMessage },
  { name: "refuses a closing parenthesis separator", files: { "menaces.md": open + "ANH)999\n" }, status: 1, message: spellingMessage },
];

for (const scenario of scenarios) {
  test(scenario.name, () => {
    // Given: isolated documents and the actual verified issue snapshot.
    const sandbox = mkdtempSync(join(tmpdir(), "anheart-men-test-"));
    const directory = join(sandbox, "docs");
    try {
      mkdirSync(directory);
      writeFileSync(join(sandbox, "proof.md"), "Synthetic sibling evidence\n");
      for (const [path, content] of Object.entries(scenario.files)) {
        writeFileSync(join(directory, path), content);
      }
      const issueFile = join(directory, "issues.tsv");
      writeFileSync(issueFile, scenario.registry ?? registry);
      // When: the same command used by the documentation CI runs.
      const result = spawnSync("bash", ["scripts/ci/check-men.sh", directory, issueFile], { encoding: "utf8" });
      // Then: it accepts covered threats and refuses broken traceability.
      assert.ifError(result.error);
      assert.equal(result.status, scenario.status, result.stderr || result.stdout);
      if (scenario.message) assert.match(result.stderr, scenario.message);
    } finally {
      rmSync(sandbox, { recursive: true });
    }
  });
}

// ANH-221: the page says, in one sentence, when the tickets of the registry
// were read, and how many rows carry each date. That sentence had fallen behind
// the registry once already, which the review of the first release found: the
// two are held together here.
test("docs/menaces.md gives the read dates the registry carries, with the number of rows of each, and no other", () => {
  // Given: the number of rows of the real registry for each date, and the sentence of the real page.
  /** @type {Map<string, number>} */
  const carried = new Map();
  for (const row of registry.split(/\r?\n/)) {
    if (!row || row.startsWith("#")) continue;
    const date = row.split("\t")[2] ?? "";
    carried.set(date, (carried.get(date) ?? 0) + 1);
  }
  const page = readFileSync(new URL("../../docs/menaces.md", import.meta.url), "utf8").replace(/\s+/g, " ");
  // When: the sentence is read from its colon up to its full stop.
  const sentence = /Chaque ligne porte sa date de lecture[^:]*:([^.]+)\./.exec(page);
  assert.ok(sentence, "the sentence that gives the read dates");
  const dates = sentence[1]?.match(/\d{4}-\d{2}-\d{2}/g) ?? [];
  const stated = new Map([...(sentence[1] ?? "").matchAll(/(\d{4}-\d{2}-\d{2}) pour (\d+) lignes?/g)].map((m) => [m[1], Number(m[2])]));
  // Then: each date comes with its number of rows, and the dates and numbers are those of the registry.
  assert.equal(stated.size, dates.length, `each date of the sentence gives its number of rows: ${sentence[1]}`);
  assert.deepEqual([...stated].sort(), [...carried].sort());
});
