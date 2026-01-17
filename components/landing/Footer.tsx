"use client";

import { Link } from "@/i18n/navigation";
import { useTranslations } from "next-intl";

export function Footer() {
  const t = useTranslations("footer");
  const tNav = useTranslations("nav");

  return (
    <footer className="border-t bg-background">
      <div className="container mx-auto px-4 py-12">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-8">
          {/* Brand */}
          <div className="col-span-2 md:col-span-1">
            <p className="font-semibold text-lg mb-4">AnHeart</p>
            <p className="text-sm text-muted-foreground leading-relaxed">
              {t("description")}
            </p>
            <p className="text-sm text-muted-foreground mt-4">
              {t("location")}
            </p>
          </div>

          {/* Product */}
          <div>
            <p className="font-medium mb-4">{t("product")}</p>
            <ul className="space-y-3 text-sm text-muted-foreground">
              <li>
                <Link
                  href="/#solution"
                  className="hover:text-foreground transition-colors"
                >
                  {tNav("solution")}
                </Link>
              </li>
              <li>
                <Link
                  href="/#technology"
                  className="hover:text-foreground transition-colors"
                >
                  {tNav("technology")}
                </Link>
              </li>
              <li>
                <Link
                  href="/#benefits"
                  className="hover:text-foreground transition-colors"
                >
                  {tNav("benefits")}
                </Link>
              </li>
              <li>
                <Link
                  href="/#markets"
                  className="hover:text-foreground transition-colors"
                >
                  {tNav("markets")}
                </Link>
              </li>
            </ul>
          </div>

          {/* Resources */}
          <div>
            <p className="font-medium mb-4">{t("resources")}</p>
            <ul className="space-y-3 text-sm text-muted-foreground">
              <li>
                <Link
                  href="/faq"
                  className="hover:text-foreground transition-colors"
                >
                  {tNav("faq")}
                </Link>
              </li>
              <li>
                <Link
                  href="/#research"
                  className="hover:text-foreground transition-colors"
                >
                  Research
                </Link>
              </li>
              <li>
                <Link
                  href="/dashboard"
                  className="hover:text-foreground transition-colors"
                >
                  {tNav("dashboard")}
                </Link>
              </li>
            </ul>
          </div>

          {/* Contact */}
          <div>
            <p className="font-medium mb-4">{t("contact")}</p>
            <ul className="space-y-3 text-sm text-muted-foreground">
              <li>
                <a
                  href="mailto:contact@anheart.com"
                  className="hover:text-foreground transition-colors"
                >
                  contact@anheart.com
                </a>
              </li>
              <li>+33 1 XX XX XX XX</li>
            </ul>
          </div>
        </div>

        <div className="border-t mt-12 pt-8 flex flex-col md:flex-row justify-between items-center gap-4">
          <p className="text-sm text-muted-foreground">
            &copy; {new Date().getFullYear()} AnHeart. {t("allRightsReserved")}
          </p>
          <div className="flex gap-6 text-sm text-muted-foreground">
            <Link
              href="/privacy"
              className="hover:text-foreground transition-colors"
            >
              {t("privacy")}
            </Link>
            <Link
              href="/terms"
              className="hover:text-foreground transition-colors"
            >
              {t("terms")}
            </Link>
          </div>
        </div>
      </div>
    </footer>
  );
}
