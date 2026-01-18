"use client";

import { useEffect } from "react";
import {
  Authenticated,
  Unauthenticated,
  useMutation,
  useQuery,
} from "convex/react";
import { api } from "@/convex/_generated/api";
import { SignUpButton, SignInButton } from "@clerk/nextjs";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import { Button } from "@/components/ui/button";
import { Navbar } from "@/components/landing/Navbar";
import { Footer } from "@/components/landing/Footer";
import { Globe } from "@/components/ui/globe";
import { NumberTicker } from "@/components/ui/number-ticker";
import { BorderBeam } from "@/components/ui/border-beam";
import { Particles } from "@/components/ui/particles";
import { ArrowRight } from "lucide-react";

export default function Home() {
  return (
    <div className="min-h-screen flex flex-col bg-background">
      <Navbar />
      <main className="flex-1">
        <HeroSection />
        <PhysicalExplanationSection />
        <HeartRateSection />
        <HowItWorksSection />
        <BenefitsSection />
        <ResearchSection />
        <CTASection />
      </main>
      <Footer />
    </div>
  );
}

function HeroSection() {
  const t = useTranslations("home");

  return (
    <section className="relative min-h-[90vh] flex items-center justify-center overflow-hidden">
      {/* Particles Background */}
      <Particles
        className="absolute inset-0"
        quantity={80}
        staticity={30}
        ease={80}
        color="#6366f1"
        size={0.5}
      />

      <BorderBeam
        size={300}
        duration={12}
        colorFrom="#6366f1"
        colorTo="#8b5cf6"
        className="opacity-40"
      />

      <div className="container mx-auto px-4 py-24 relative z-10">
        <div className="grid lg:grid-cols-2 gap-12 lg:gap-16 items-center">
          <div className="space-y-8 max-w-xl">
            <p className="text-sm font-medium text-primary tracking-wide uppercase">
              {t("badge")}
            </p>

            <h1 className="text-4xl sm:text-5xl md:text-6xl lg:text-7xl font-semibold leading-[1.1] tracking-tight">
              {t("title")}
            </h1>

            <p className="text-lg sm:text-xl text-muted-foreground leading-relaxed">
              {t("description")}
            </p>

            <div className="flex flex-col sm:flex-row gap-4 pt-4">
              <Unauthenticated>
                <SignUpButton mode="modal">
                  <Button size="lg" className="px-8">
                    {t("getStarted")}
                    <ArrowRight className="ml-2 h-4 w-4" />
                  </Button>
                </SignUpButton>
                <SignInButton mode="modal">
                  <Button size="lg" variant="outline" className="px-8">
                    {t("signIn")}
                  </Button>
                </SignInButton>
              </Unauthenticated>
              <Authenticated>
                <AuthenticatedHero />
              </Authenticated>
            </div>

            <div className="flex gap-8 sm:gap-12 pt-8">
              <div>
                <div className="text-3xl sm:text-4xl font-semibold tracking-tight">
                  <NumberTicker value={40} />+
                </div>
                <p className="text-xs sm:text-sm text-muted-foreground mt-1">
                  {t("yearsResearch")}
                </p>
              </div>
              <div>
                <div className="text-3xl sm:text-4xl font-semibold tracking-tight">
                  <NumberTicker value={30} />+
                </div>
                <p className="text-xs sm:text-sm text-muted-foreground mt-1">
                  {t("publishedPapers")}
                </p>
              </div>
              <div>
                <div className="text-3xl sm:text-4xl font-semibold tracking-tight">
                  <NumberTicker value={150} />+
                </div>
                <p className="text-xs sm:text-sm text-muted-foreground mt-1">
                  {t("proClubs")}
                </p>
              </div>
            </div>
          </div>

          {/* Globe - now visible on all screen sizes */}
          <div className="relative h-[300px] sm:h-[400px] lg:h-[500px]">
            <Globe className="absolute inset-0" />
            <div className="absolute bottom-4 sm:bottom-8 left-1/2 -translate-x-1/2 bg-card/90 backdrop-blur-sm rounded-lg px-4 sm:px-5 py-2 sm:py-3">
              <p className="text-xs sm:text-sm font-medium text-center">
                {t("basedIn")}
              </p>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function AuthenticatedHero() {
  const t = useTranslations("home");
  const user = useQuery(api.users.getCurrentUser);
  const syncUser = useMutation(api.users.getOrCreateUser);

  useEffect(() => {
    if (user === null) {
      syncUser();
    }
  }, [user, syncUser]);

  return (
    <Link href="/dashboard">
      <Button size="lg" className="px-8">
        {t("goToDashboard")}
        <ArrowRight className="ml-2 h-4 w-4" />
      </Button>
    </Link>
  );
}

function PhysicalExplanationSection() {
  const t = useTranslations("home.solution");

  const steps = [
    {
      number: "01",
      title: t("step1Title"),
      description: t("step1Description"),
    },
    {
      number: "02",
      title: t("step2Title"),
      description: t("step2Description"),
    },
    {
      number: "03",
      title: t("step3Title"),
      description: t("step3Description"),
    },
  ];

  return (
    <section id="solution" className="py-24 lg:py-32 relative overflow-hidden">
      <BorderBeam
        size={400}
        duration={15}
        colorFrom="#6366f1"
        colorTo="#a855f7"
        className="opacity-30"
        reverse
      />

      <div className="container mx-auto px-4 relative">
        <div className="max-w-3xl mb-16 lg:mb-20">
          <p className="text-sm font-medium text-primary tracking-wide uppercase mb-4">
            {t("badge")}
          </p>
          <h2 className="text-3xl sm:text-4xl md:text-5xl font-semibold tracking-tight mb-6">
            {t("title")}
          </h2>
          <p className="text-lg sm:text-xl text-muted-foreground leading-relaxed">
            {t("description")}
          </p>
        </div>

        <div className="grid sm:grid-cols-2 md:grid-cols-3 gap-6 lg:gap-8">
          {steps.map((step) => (
            <div key={step.number} className="p-6 lg:p-8 rounded-2xl bg-card">
              <span className="text-4xl lg:text-5xl font-light text-muted-foreground/30">
                {step.number}
              </span>
              <h3 className="text-lg lg:text-xl font-semibold mt-4 mb-3">
                {step.title}
              </h3>
              <p className="text-muted-foreground leading-relaxed">
                {step.description}
              </p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function HeartRateSection() {
  const t = useTranslations("home.heartRate");

  const states = [
    {
      title: t("lyingDown"),
      bpm: "40-60",
      description: t("lyingDownDesc"),
    },
    {
      title: t("standingUp"),
      bpm: "60-80",
      description: t("standingUpDesc"),
    },
    {
      title: t("training"),
      bpm: "40-160",
      description: t("trainingDesc"),
      featured: true,
    },
  ];

  return (
    <section className="py-24 lg:py-32 bg-muted/30">
      <div className="container mx-auto px-4">
        <div className="max-w-3xl mx-auto text-center mb-16 lg:mb-20">
          <p className="text-sm font-medium text-primary tracking-wide uppercase mb-4">
            {t("badge")}
          </p>
          <h2 className="text-3xl sm:text-4xl md:text-5xl font-semibold tracking-tight mb-6">
            {t("title")}
          </h2>
          <p className="text-lg sm:text-xl text-muted-foreground leading-relaxed">
            {t("description")}
          </p>
        </div>

        <div className="grid sm:grid-cols-2 md:grid-cols-3 gap-6 lg:gap-8 max-w-4xl mx-auto">
          {states.map((state) => (
            <div
              key={state.title}
              className={`p-6 lg:p-8 rounded-2xl text-center ${
                state.featured
                  ? "bg-primary text-primary-foreground"
                  : "bg-card"
              }`}
            >
              <h3
                className={`text-base lg:text-lg font-medium mb-4 ${state.featured ? "" : "text-muted-foreground"}`}
              >
                {state.title}
              </h3>
              <div className="text-4xl lg:text-5xl font-semibold tracking-tight mb-4">
                {state.bpm}
              </div>
              <p className="text-sm">{t("bpm")}</p>
              <p
                className={`text-sm mt-4 ${state.featured ? "text-primary-foreground/80" : "text-muted-foreground"}`}
              >
                {state.description}
              </p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function HowItWorksSection() {
  const t = useTranslations("home.technology");

  return (
    <section
      id="technology"
      className="py-24 lg:py-32 relative overflow-hidden"
    >
      <BorderBeam
        size={350}
        duration={18}
        colorFrom="#8b5cf6"
        colorTo="#6366f1"
        className="opacity-25"
      />

      <div className="container mx-auto px-4 relative">
        <div className="grid lg:grid-cols-2 gap-12 lg:gap-20 items-center">
          <div>
            <p className="text-sm font-medium text-primary tracking-wide uppercase mb-4">
              {t("badge")}
            </p>
            <h2 className="text-3xl sm:text-4xl md:text-5xl font-semibold tracking-tight mb-6">
              {t("title")}
            </h2>
            <p className="text-lg sm:text-xl text-muted-foreground leading-relaxed mb-8 lg:mb-12">
              {t("description")}
            </p>

            <div className="space-y-6 lg:space-y-8">
              <div>
                <h3 className="text-base lg:text-lg font-semibold mb-2">
                  {t("step1Title")}
                </h3>
                <p className="text-muted-foreground leading-relaxed">
                  {t("step1Description")}
                </p>
              </div>

              <div>
                <h3 className="text-base lg:text-lg font-semibold mb-2">
                  {t("step2Title")}
                </h3>
                <p className="text-muted-foreground leading-relaxed">
                  {t("step2Description")}
                </p>
              </div>

              <div>
                <h3 className="text-base lg:text-lg font-semibold mb-2">
                  {t("step3Title")}
                </h3>
                <p className="text-muted-foreground leading-relaxed">
                  {t("step3Description")}
                </p>
              </div>
            </div>
          </div>

          <div className="flex items-center justify-center">
            <div className="p-8 lg:p-12 rounded-3xl bg-card text-center max-w-sm w-full">
              <p className="text-sm text-muted-foreground uppercase tracking-wide mb-4">
                {t("artificialGravity")}
              </p>
              <div className="text-6xl lg:text-8xl font-semibold tracking-tight text-primary">
                3x
              </div>
              <p className="text-muted-foreground mt-4">{t("earthGravity")}</p>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function BenefitsSection() {
  const t = useTranslations("home.benefits");

  const benefits = [
    { title: t("heartTraining"), description: t("heartTrainingDesc") },
    { title: t("recovery"), description: t("recoveryDesc") },
    { title: t("vo2max"), description: t("vo2maxDesc") },
    { title: t("vascularization"), description: t("vascularizationDesc") },
    { title: t("injuries"), description: t("injuriesDesc") },
    { title: t("balance"), description: t("balanceDesc") },
    { title: t("muscle"), description: t("muscleDesc") },
    { title: t("time"), description: t("timeDesc") },
  ];

  return (
    <section id="benefits" className="py-24 lg:py-32 bg-muted/30">
      <div className="container mx-auto px-4">
        <div className="max-w-3xl mx-auto text-center mb-16 lg:mb-20">
          <p className="text-sm font-medium text-primary tracking-wide uppercase mb-4">
            {t("badge")}
          </p>
          <h2 className="text-3xl sm:text-4xl md:text-5xl font-semibold tracking-tight mb-6">
            {t("title")}
          </h2>
          <p className="text-lg sm:text-xl text-muted-foreground leading-relaxed">
            {t("description")}
          </p>
        </div>

        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4 lg:gap-6">
          {benefits.map((benefit) => (
            <div key={benefit.title} className="p-5 lg:p-6 rounded-2xl bg-card">
              <h3 className="font-semibold mb-2">{benefit.title}</h3>
              <p className="text-sm text-muted-foreground leading-relaxed">
                {benefit.description}
              </p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function ResearchSection() {
  const t = useTranslations("home.research");

  return (
    <section id="research" className="py-24 lg:py-32 relative overflow-hidden">
      <BorderBeam
        size={300}
        duration={14}
        colorFrom="#6366f1"
        colorTo="#8b5cf6"
        className="opacity-20"
        reverse
      />

      <div className="container mx-auto px-4 relative">
        <div className="grid lg:grid-cols-2 gap-12 lg:gap-20 items-center">
          <div>
            <p className="text-sm font-medium text-primary tracking-wide uppercase mb-4">
              {t("badge")}
            </p>
            <h2 className="text-3xl sm:text-4xl md:text-5xl font-semibold tracking-tight mb-6">
              {t("title")}
            </h2>
            <p className="text-lg sm:text-xl text-muted-foreground leading-relaxed mb-8">
              {t("description")}
            </p>

            <div className="space-y-4 lg:space-y-6">
              <div className="p-5 lg:p-6 rounded-2xl bg-card">
                <h3 className="font-semibold mb-2">{t("clinicalResults")}</h3>
                <p className="text-muted-foreground mb-4">
                  {t("clinicalResultsDesc")}
                </p>
                <div className="flex gap-8 lg:gap-12">
                  <div>
                    <div className="text-2xl lg:text-3xl font-semibold">
                      x15
                    </div>
                    <p className="text-sm text-muted-foreground">
                      {t("walkingMobility")}
                    </p>
                  </div>
                  <div>
                    <div className="text-2xl lg:text-3xl font-semibold">x7</div>
                    <p className="text-sm text-muted-foreground">
                      {t("cyclingMobility")}
                    </p>
                  </div>
                </div>
              </div>

              <div className="p-5 lg:p-6 rounded-2xl bg-card">
                <p className="text-muted-foreground">{t("caseStudy")}</p>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4 lg:gap-6">
            <div className="p-6 lg:p-8 rounded-2xl bg-card text-center">
              <div className="text-4xl lg:text-5xl font-semibold tracking-tight">
                <NumberTicker value={30} />+
              </div>
              <p className="text-muted-foreground mt-2">
                {t("researchPapers")}
              </p>
            </div>
            <div className="p-6 lg:p-8 rounded-2xl bg-card text-center">
              <div className="text-4xl lg:text-5xl font-semibold tracking-tight">
                <NumberTicker value={40} />
              </div>
              <p className="text-muted-foreground mt-2">{t("yearsUruguay")}</p>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function CTASection() {
  const t = useTranslations("home.cta");
  const tHome = useTranslations("home");

  return (
    <section className="py-24 lg:py-32 relative overflow-hidden">
      <Particles
        className="absolute inset-0"
        quantity={50}
        staticity={40}
        ease={80}
        color="#6366f1"
        size={0.4}
      />

      <BorderBeam
        size={400}
        duration={16}
        colorFrom="#8b5cf6"
        colorTo="#6366f1"
        className="opacity-30"
      />

      <div className="container mx-auto px-4 relative">
        <div className="max-w-3xl mx-auto text-center">
          <h2 className="text-3xl sm:text-4xl md:text-5xl font-semibold tracking-tight mb-6">
            {t("title")}
          </h2>
          <p className="text-lg sm:text-xl text-muted-foreground mb-8 lg:mb-10 leading-relaxed">
            {t("description")}
          </p>
          <div className="flex flex-col sm:flex-row gap-4 justify-center">
            <Unauthenticated>
              <SignUpButton mode="modal">
                <Button size="lg" className="px-8">
                  {t("getStarted")}
                  <ArrowRight className="ml-2 h-4 w-4" />
                </Button>
              </SignUpButton>
              <Link href="/faq">
                <Button size="lg" variant="outline" className="px-8">
                  {t("learnMore")}
                </Button>
              </Link>
            </Unauthenticated>
            <Authenticated>
              <Link href="/dashboard">
                <Button size="lg" className="px-8">
                  {tHome("goToDashboard")}
                  <ArrowRight className="ml-2 h-4 w-4" />
                </Button>
              </Link>
            </Authenticated>
          </div>
        </div>
      </div>
    </section>
  );
}
