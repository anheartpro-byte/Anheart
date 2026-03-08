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
        <UseCasesSection />
        <BenefitsSection />
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

function UseCasesSection() {
  const t = useTranslations("home.useCases");

  return (
    <section id="usecases" className="py-24 lg:py-32 relative overflow-hidden">
      <BorderBeam size={350} duration={18} colorFrom="#8b5cf6" colorTo="#6366f1" className="opacity-25" />
      <div className="container mx-auto px-4 relative">
        <div className="max-w-3xl mb-16 lg:mb-20">
          <p className="text-sm font-medium text-primary tracking-wide uppercase mb-4">{t("badge")}</p>
          <h2 className="text-3xl sm:text-4xl md:text-5xl font-semibold tracking-tight mb-6">{t("title")}</h2>
          <p className="text-lg sm:text-xl text-muted-foreground leading-relaxed">{t("description")}</p>
        </div>

        {/* Maintain physical activity */}
        <div className="mb-16">
          <h3 className="text-2xl font-semibold mb-8">{t("maintainTitle")}</h3>
          <p className="text-muted-foreground mb-8">{t("maintainDesc")}</p>
          <div className="grid sm:grid-cols-2 md:grid-cols-3 gap-6 lg:gap-8">
            <div className="p-6 lg:p-8 rounded-2xl bg-card">
              <h4 className="text-lg font-semibold mb-4">{t("injuredAthletes")}</h4>
              <ul className="space-y-2 text-muted-foreground">
                <li>{t("injuredAthletes1")}</li>
                <li>{t("injuredAthletes2")}</li>
                <li>{t("injuredAthletes3")}</li>
              </ul>
            </div>
            <div className="p-6 lg:p-8 rounded-2xl bg-card">
              <h4 className="text-lg font-semibold mb-4">{t("elderly")}</h4>
              <ul className="space-y-2 text-muted-foreground">
                <li>{t("elderly1")}</li>
                <li>{t("elderly2")}</li>
                <li>{t("elderly3")}</li>
              </ul>
            </div>
            <div className="p-6 lg:p-8 rounded-2xl bg-card">
              <h4 className="text-lg font-semibold mb-4">{t("disabled")}</h4>
              <ul className="space-y-2 text-muted-foreground">
                <li>{t("disabled1")}</li>
                <li>{t("disabled2")}</li>
                <li>{t("disabled3")}</li>
              </ul>
            </div>
          </div>
        </div>

        {/* Optimize training */}
        <div className="mb-16">
          <h3 className="text-2xl font-semibold mb-4">{t("optimizeTitle")}</h3>
          <p className="text-muted-foreground mb-6">{t("optimizeDesc")}</p>
          <ul className="space-y-2 text-muted-foreground mb-8 list-disc list-inside">
            <li>{t("optimize1")}</li>
            <li>{t("optimize2")}</li>
            <li>{t("optimize3")}</li>
            <li>{t("optimize4")}</li>
          </ul>
          <div className="grid sm:grid-cols-2 gap-6 lg:gap-8">
            <div className="p-6 lg:p-8 rounded-2xl bg-card">
              <h4 className="text-lg font-semibold mb-4">{t("eliteAthletes")}</h4>
              <ul className="space-y-2 text-muted-foreground">
                <li>{t("eliteAthletes1")}</li>
                <li>{t("eliteAthletes2")}</li>
                <li>{t("eliteAthletes3")}</li>
              </ul>
            </div>
            <div className="p-6 lg:p-8 rounded-2xl bg-card">
              <h4 className="text-lg font-semibold mb-4">{t("regularAthletes")}</h4>
              <ul className="space-y-2 text-muted-foreground">
                <li>{t("regularAthletes1")}</li>
                <li>{t("regularAthletes2")}</li>
                <li>{t("regularAthletes3")}</li>
              </ul>
            </div>
          </div>
        </div>

        {/* Bottom paragraph */}
        <div className="p-6 lg:p-8 rounded-2xl bg-primary/5 border border-primary/10">
          <p className="text-muted-foreground leading-relaxed">{t("ambition")}</p>
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
