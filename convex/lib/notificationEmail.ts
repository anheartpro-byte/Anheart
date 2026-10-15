/**
 * The e-mail that may go with a new alert (ANH-90), through the Resend
 * component (`@convex-dev/resend`, mounted by `convex/convex.config.ts`).
 *
 * Off unless the deployment sets all three variables:
 * - `RESEND_API_KEY`: the key of the Resend account;
 * - `NOTIFICATION_EMAIL_FROM`: the sender, an address of a domain verified
 *   in that account (`Gaura <alertes@example.org>`);
 * - `SITE_URL`: the site's address in https, which the e-mail links to.
 * With one of them missing no e-mail is queued and the alerts stay on the site.
 *
 * What an e-mail says is written here and nowhere else: the kind of alert,
 * the machine's name, and a link to the alerts page of the site. Never a
 * heart rate, a passenger's name, a session's reason or an identifier: the
 * page behind the link shows them to whoever may read them.
 */
import { Resend } from "@convex-dev/resend";
import { components } from "../_generated/api";
import type { Doc } from "../_generated/dataModel";
import type { MutationCtx } from "../_generated/server";

export type NotificationType = Doc<"notifications">["type"];
export type Language = Doc<"users">["language"];

/** What sending needs, read from the deployment's environment. */
export type EmailSettings = { apiKey: string; from: string; siteUrl: string };

/** The settings of the e-mails, or null when one of the three is missing. */
export function emailSettings(): EmailSettings | null {
  const apiKey = (process.env.RESEND_API_KEY ?? "").trim();
  const from = (process.env.NOTIFICATION_EMAIL_FROM ?? "").trim();
  const siteUrl = (process.env.SITE_URL ?? "").trim().replace(/\/+$/, "");
  if (apiKey === "" || from === "") return null;
  // An address the link can be built on: https, a host, nothing after it
  // but a path.
  if (!/^https:\/\/[A-Za-z0-9.-]+(?::\d+)?(?:\/[^\s?#]*)?$/.test(siteUrl)) {
    return null;
  }
  return { apiKey, from, siteUrl };
}

/** The longest machine name an e-mail carries, in characters. */
const MAX_NAME_LENGTH = 80;

/**
 * A machine's name as an e-mail may show it: on one line, without control
 * characters, cut to `MAX_NAME_LENGTH`.
 */
export function mailableName(name: string): string {
  const flat = name
    .replace(/[\p{Cc}\p{Zl}\p{Zp}]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
  return flat.length > MAX_NAME_LENGTH
    ? `${flat.slice(0, MAX_NAME_LENGTH - 1)}…`
    : flat;
}

const WORDS: Record<
  Language,
  {
    types: Record<NotificationType, string>;
    subject: (what: string, machine: string) => string;
    body: (what: string, machine: string, link: string) => string;
  }
> = {
  fr: {
    types: {
      machine_offline: "Machine hors ligne",
      session_failed: "Séance échouée",
      drive_fault: "Défaut variateur",
      safety_verdict: "Arrêt demandé par la sécurité",
    },
    subject: (what, machine) => `[Gaura] ${what} : ${machine}`,
    body: (what, machine, link) =>
      [
        `${what} : machine « ${machine} ».`,
        "",
        `Le détail est sur la page des alertes : ${link}`,
        "",
        "Vous recevez cet e-mail parce que vous gérez cette machine. Les alertes envoyées par e-mail se choisissent sur cette même page.",
      ].join("\n"),
  },
  en: {
    types: {
      machine_offline: "Machine offline",
      session_failed: "Session failed",
      drive_fault: "Drive fault",
      safety_verdict: "Stop requested by the safety layer",
    },
    subject: (what, machine) => `[Gaura] ${what}: ${machine}`,
    body: (what, machine, link) =>
      [
        `${what}: machine "${machine}".`,
        "",
        `The details are on the alerts page: ${link}`,
        "",
        "You receive this email because you manage this machine. The alerts sent by email are chosen on that same page.",
      ].join("\n"),
  },
};

/** The subject and the text of the e-mail of an alert, in the recipient's language. */
export function alertEmail(
  type: NotificationType,
  language: Language,
  machineName: string,
  siteUrl: string,
): { subject: string; text: string } {
  const words = WORDS[language];
  const what = words.types[type];
  const machine = mailableName(machineName);
  const link = `${siteUrl}/${language}/dashboard/notifications`;
  return {
    subject: words.subject(what, machine),
    text: words.body(what, machine, link),
  };
}

/**
 * Queue the e-mail of one alert; the component sends it, retries it, and
 * never sends twice an e-mail queued with the same `idempotencyKey`.
 * Returns the component's identifier of the e-mail.
 */
export async function queueAlertEmail(
  ctx: MutationCtx,
  settings: EmailSettings,
  to: string,
  content: { subject: string; text: string },
  idempotencyKey: string,
): Promise<string> {
  // `testMode: false`: the addresses are real ones. The key is passed here,
  // read with the two other variables, never left to the component to read.
  const resend = new Resend(components.resend, {
    apiKey: settings.apiKey,
    testMode: false,
  });
  return await resend.sendEmail(ctx, {
    from: settings.from,
    to,
    subject: content.subject,
    text: content.text,
    idempotencyKey,
  });
}
