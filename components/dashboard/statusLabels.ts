"use client";

import { useMessages, useTranslations } from "next-intl";

/**
 * True when `path` reaches a message through own properties only, so a raw
 * value such as "constructor" or "toString" is never mistaken for a known key.
 */
export function hasOwnMessage(
  messages: unknown,
  path: ReadonlyArray<string>,
): boolean {
  let node: unknown = messages;
  for (const key of path) {
    if (
      typeof node !== "object" ||
      node === null ||
      !Object.prototype.hasOwnProperty.call(node, key)
    ) {
      return false;
    }
    node = (node as Record<string, unknown>)[key];
  }
  return typeof node === "string";
}

/**
 * Translate a raw enumerated value through `<namespace>.<value>` of the
 * catalog. A value the catalog does not list is returned as received: never
 * hidden, never replaced by a key path.
 */
export function useCatalogLabel(namespace: string) {
  const messages = useMessages();
  const t = useTranslations(namespace);
  const path = namespace.split(".");
  return (value: string) =>
    hasOwnMessage(messages, [...path, value]) ? t(value) : value;
}

/** Translate the raw session status, falling back to the raw value. */
export function useSessionStatusLabel() {
  return useCatalogLabel("sessions.status");
}

const MACHINE_STATUS_KEYS = new Map([
  ["online", "online"],
  ["offline", "offline"],
  ["in_session", "inSession"],
]);

/** Translate the raw machine status, falling back to the raw value. */
export function useMachineStatusLabel() {
  const t = useTranslations("machines");
  return (status: string) => {
    const key = MACHINE_STATUS_KEYS.get(status);
    return key === undefined ? status : t(key);
  };
}
