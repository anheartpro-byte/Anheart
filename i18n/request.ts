import { getRequestConfig } from "next-intl/server";
import { routing } from "./routing";

export default getRequestConfig(async ({ requestLocale }) => {
  const requestedLocale = await requestLocale;

  // Ensure that the incoming locale is valid
  const locale =
    requestedLocale && routing.locales.includes(requestedLocale as "fr" | "en")
      ? requestedLocale
      : routing.defaultLocale;

  return {
    locale,
    messages: (await import(`../messages/${locale}.json`)).default,
  };
});
