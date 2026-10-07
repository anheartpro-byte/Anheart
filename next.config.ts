import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";
import packageJson from "./package.json";

const withNextIntl = createNextIntlPlugin("./i18n/request.ts");

const nextConfig: NextConfig = {
  // ANH-134: the version of the site, read by `lib/version.ts` and shown in the
  // footer. `scripts/release.sh` writes it into package.json.
  env: {
    NEXT_PUBLIC_WEB_VERSION: `web-${packageJson.version}`,
  },
};

export default withNextIntl(nextConfig);
