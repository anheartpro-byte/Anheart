"use client";

import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import { ArrowLeft, FileText } from "lucide-react";

export default function TermsPage() {
  const t = useTranslations("terms");

  const sections = [
    { title: t("section1Title"), content: t("section1Content") },
    { title: t("section2Title"), content: t("section2Content") },
    { title: t("section3Title"), content: t("section3Content") },
    { title: t("section4Title"), content: t("section4Content") },
    { title: t("section5Title"), content: t("section5Content") },
    { title: t("section6Title"), content: t("section6Content") },
    { title: t("section7Title"), content: t("section7Content") },
    { title: t("section8Title"), content: t("section8Content") },
    { title: t("section9Title"), content: t("section9Content") },
    { title: t("section10Title"), content: t("section10Content") },
  ];

  return (
    <div className="min-h-screen bg-background">
      <div className="container mx-auto px-4 py-12 max-w-4xl">
        {/* Header */}
        <div className="mb-8">
          <Link href="/">
            <Button variant="ghost" size="sm" className="mb-4">
              <ArrowLeft className="h-4 w-4 mr-2" />
              {t("backToHome")}
            </Button>
          </Link>
          <div className="flex items-center gap-3 mb-4">
            <div className="p-2 rounded-lg bg-primary/10">
              <FileText className="h-6 w-6 text-primary" />
            </div>
            <h1 className="text-3xl font-bold">{t("title")}</h1>
          </div>
          <p className="text-muted-foreground">
            {t("lastUpdated")}: January 17, 2026
          </p>
        </div>

        {/* Introduction */}
        <div className="prose prose-neutral dark:prose-invert max-w-none mb-8">
          <p className="text-lg leading-relaxed">{t("intro")}</p>
        </div>

        {/* Sections */}
        <div className="space-y-8">
          {sections.map((section, index) => (
            <section key={index} className="border-b pb-8 last:border-b-0">
              <h2 className="text-xl font-semibold mb-4">
                {index + 1}. {section.title}
              </h2>
              <p className="text-muted-foreground leading-relaxed">
                {section.content}
              </p>
            </section>
          ))}

          {/* Contact Section */}
          <section className="bg-muted/50 rounded-lg p-6">
            <p className="text-muted-foreground mb-4">
              {t("section10Content")}
            </p>
            <a
              href="mailto:contact@gauratechnologies.com"
              className="text-primary hover:underline font-medium"
            >
              contact@gauratechnologies.com
            </a>
          </section>
        </div>

        {/* Footer */}
        <div className="mt-12 pt-8 border-t text-center">
          <p className="text-sm text-muted-foreground">
            &copy; {new Date().getFullYear()} Gaura. All rights reserved.
          </p>
        </div>
      </div>
    </div>
  );
}
