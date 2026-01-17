import createMiddleware from "next-intl/middleware";
import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { routing } from "./i18n/routing";

const intlMiddleware = createMiddleware(routing);

const isProtectedRoute = createRouteMatcher([
  "/:locale/dashboard(.*)",
  "/:locale/settings(.*)",
]);

export default clerkMiddleware(async (auth, req) => {
  if (isProtectedRoute(req)) {
    await auth.protect();
  }

  return intlMiddleware(req);
});

export const config = {
  matcher: [
    // Match all pathnames except for
    // - API routes (/api/...)
    // - static files (/_next/static/..., /_next/image/..., /favicon.ico, etc.)
    // - convex routes
    "/((?!api|_next|convex|.*\\..*).*)",
    "/",
    "/(fr|en)/:path*",
  ],
};
