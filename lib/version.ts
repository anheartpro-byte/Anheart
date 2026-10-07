/**
 * Version of the site, for example "web-0.1.0" (ANH-134).
 *
 * `next.config.ts` builds it from the `version` of `package.json`, which
 * `scripts/release.sh` writes, and Next.js inlines it at build time.
 */
export const WEB_VERSION: string =
  process.env.NEXT_PUBLIC_WEB_VERSION ?? "web-inconnue";
