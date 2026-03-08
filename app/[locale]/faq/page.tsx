"use client";

import { Navbar } from "@/components/landing/Navbar";
import { Footer } from "@/components/landing/Footer";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Button } from "@/components/ui/button";
import { Link } from "@/i18n/navigation";
import { BorderBeam } from "@/components/ui/border-beam";
import { useTranslations, useLocale } from "next-intl";
import { ArrowRight } from "lucide-react";

// FAQ data is kept in English/French based on locale
const faqData = {
  en: [
    {
      category: "General Questions",
      faqs: [
        {
          question: "What is Gaura?",
          answer:
            "Gaura is a revolutionary cardiovascular training system that uses centrifugal force to create artificial gravity. This technology enables effective heart training, recovery improvement, and various medical rehabilitation applications.",
        },
        {
          question: "How does artificial gravity training work?",
          answer:
            "The Gaura machine rotates to create centrifugal force, which pushes blood towards the lower extremities. This simulates gravitational effects and forces the heart to work harder to bring blood from the feet back to the head. This cardiovascular training in a lying position reproduces the body's reaction when standing or performing physical exercise like jogging.",
        },
        {
          question: "Is Gaura technology scientifically proven?",
          answer:
            "Gaura technology is based on over 40 years of research in Uruguay and from globally recognized entities in the space industry. Existing studies have shown that artificial gravity therapy significantly improves vascularization, mobility, and helps maintain cardiovascular health.",
        },
        {
          question: "Where is Gaura based?",
          answer:
            "Gaura is headquartered in Paris, France.",
        },
      ],
    },
    {
      category: "Technology & Training",
      faqs: [
        {
          question: "What is the recommended training duration?",
          answer:
            "The recommended training protocol is 30-40 minutes per day at 1-3g (times Earth's gravity). The exact duration and intensity are personalized based on individual health conditions and training goals.",
        },
        {
          question: "What heart rate can be achieved during training?",
          answer:
            "During Gaura training, heart rates can range from 40-160 BPM, compared to 40-60 BPM lying down and 60-80 BPM standing up. This wide range allows for highly customizable and effective cardiovascular training.",
        },
        {
          question: "How is the training intensity controlled?",
          answer:
            "The heartbeat is directly linked to the speed of rotation. By adjusting the rotation speed, practitioners can precisely control the training intensity and achieve specific heart rate targets for optimal results.",
        },
        {
          question: "What channels does the ECG monitoring system track?",
          answer:
            "Our ECG monitoring system can track multiple channels including ECG, EMG, EDA, EEG, ACC, and LUX. This comprehensive monitoring ensures safe and effective training sessions with real-time feedback.",
        },
      ],
    },
    {
      category: "Athletes & Performance",
      faqs: [
        {
          question: "How can athletes benefit from Gaura training?",
          answer:
            "Athletes benefit through improved cardiovascular endurance, faster recovery from injuries, enhanced VO2 Max, better blood circulation, maintained fitness during injury rehabilitation, and reduced risk of future injuries.",
        },
        {
          question: "Can Gaura technology be used during injury recovery?",
          answer:
            "Yes, it can be particularly valuable during injury recovery as it allows athletes to maintain cardiovascular fitness and continue heart training without putting stress on injured body parts.",
        },
      ],
    },
  ],
  fr: [
    {
      category: "Questions Générales",
      faqs: [
        {
          question: "Qu'est-ce que Gaura ?",
          answer:
            "Gaura est un système révolutionnaire d'entraînement cardiovasculaire qui utilise la force centrifuge pour créer une gravité artificielle. Cette technologie permet un entraînement cardiaque efficace, une amélioration de la récupération et diverses applications de rééducation médicale.",
        },
        {
          question:
            "Comment fonctionne l'entraînement par gravité artificielle ?",
          answer:
            "La machine Gaura tourne pour créer une force centrifuge, qui pousse le sang vers les extrémités inférieures. Cela simule les effets gravitationnels et force le cœur à travailler plus dur pour ramener le sang des pieds à la tête. Cet entraînement cardiovasculaire en position allongée permet de reproduire la réaction du corps lorsqu'il est en position debout ou qu'il réalise un exercice physique comme un footing.",
        },
        {
          question: "La technologie Gaura est-elle scientifiquement prouvée ?",
          answer:
            "La technologie Gaura s'appuie sur des recherches de plus de 40 ans en Uruguay ou d'entités mondialement connues dans le spatial. Les études existantes ont montré que la thérapie par gravité artificielle améliore significativement la vascularisation, la mobilité et permet de maintenir la santé cardiovasculaire.",
        },
        {
          question: "Où est basée Gaura ?",
          answer:
            "Gaura a son siège à Paris, en France.",
        },
      ],
    },
    {
      category: "Technologie et Entraînement",
      faqs: [
        {
          question: "Quelle est la durée d'entraînement ?",
          answer:
            "Le protocole d'entraînement recommandé est de 30 à 40 minutes par jour à 1-3g (fois la gravité terrestre). La durée et l'intensité exactes sont personnalisées en fonction des conditions de santé individuelles et des objectifs d'entraînement.",
        },
        {
          question:
            "Quelle fréquence cardiaque peut être atteinte pendant l'entraînement?",
          answer:
            "Pendant l'entraînement Gaura, les fréquences cardiaques peuvent varier de 40 à 160 BPM, contre 40-60 BPM en position allongée et 60-80 BPM debout. Cette large gamme permet un entraînement cardiovasculaire hautement personnalisable et efficace.",
        },
        {
          question: "Comment l'intensité de l'entraînement est-elle contrôlée?",
          answer:
            "Le rythme cardiaque est directement lié à la vitesse de rotation. En ajustant la vitesse de rotation, les praticiens peuvent contrôler précisément l'intensité de l'entraînement et atteindre des objectifs de fréquence cardiaque spécifiques pour des résultats optimaux.",
        },
        {
          question: "Quels canaux le système de surveillance ECG suit-il?",
          answer:
            "Notre système de surveillance ECG peut suivre plusieurs canaux, notamment ECG, EMG, EDA, EEG, ACC et LUX. Cette surveillance complète garantit des sessions d'entraînement sûres et efficaces avec un retour en temps réel.",
        },
      ],
    },
    {
      category: "Athlètes et Performance",
      faqs: [
        {
          question:
            "Comment les athlètes peuvent-ils bénéficier de l'entraînement Gaura?",
          answer:
            "Les athlètes bénéficient d'une endurance cardiovasculaire améliorée, d'une récupération plus rapide après les blessures, d'un VO2 Max amélioré, d'une meilleure circulation sanguine, d'un maintien de la forme pendant la rééducation après blessure et d'un risque réduit de blessures futures.",
        },
        {
          question:
            "La technologie Gaura peut-elle être utilisée pendant la récupération après une blessure?",
          answer:
            "Oui, elle peut être particulièrement précieuse pendant la récupération après une blessure car il permet aux athlètes de maintenir leur forme cardiovasculaire et de continuer l'entraînement cardiaque sans solliciter les parties du corps blessées.",
        },
      ],
    },
  ],
};

export default function FAQPage() {
  const t = useTranslations("faq");
  const locale = useLocale();
  const data = faqData[locale as keyof typeof faqData] || faqData.en;

  return (
    <div className="min-h-screen flex flex-col bg-background">
      <Navbar />
      <main className="flex-1">
        {/* Hero Section */}
        <section className="relative py-24 overflow-hidden">
          <BorderBeam
            size={300}
            duration={12}
            colorFrom="#6366f1"
            colorTo="#8b5cf6"
            className="opacity-30"
          />

          <div className="container mx-auto px-4 relative">
            <div className="max-w-3xl mx-auto text-left">
              <p className="text-sm font-medium text-primary tracking-wide uppercase mb-4">
                {t("badge")}
              </p>
              <h1 className="text-4xl md:text-5xl font-semibold tracking-tight mb-6">
                {t("title")}
              </h1>
              <p className="text-xl text-muted-foreground leading-relaxed">
                {t("description")}
              </p>
            </div>
          </div>
        </section>

        {/* FAQ Content */}
        <section className="py-20">
          <div className="container mx-auto px-4 max-w-4xl">
            <div className="space-y-12">
              {data.map((category, categoryIndex) => (
                <div key={categoryIndex}>
                  <h2 className="text-2xl font-semibold mb-6 text-left">
                    {category.category}
                  </h2>
                  <div className="rounded-2xl bg-card p-6">
                    <Accordion type="single" collapsible className="w-full">
                      {category.faqs.map((faq, faqIndex) => (
                        <AccordionItem
                          key={faqIndex}
                          value={`${categoryIndex}-${faqIndex}`}
                          className="border-b border-border/50 last:border-0"
                        >
                          <AccordionTrigger className="text-left py-4 hover:no-underline">
                            {faq.question}
                          </AccordionTrigger>
                          <AccordionContent className="text-muted-foreground leading-relaxed pb-4">
                            {faq.answer}
                          </AccordionContent>
                        </AccordionItem>
                      ))}
                    </Accordion>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* CTA Section */}
        <section className="py-20 bg-muted/30">
          <div className="container mx-auto px-4">
            <div className="max-w-2xl mx-auto text-center">
              <h2 className="text-3xl font-semibold tracking-tight mb-4">
                {t("stillQuestions")}
              </h2>
              <p className="text-muted-foreground mb-8">
                {t("stillQuestionsDesc")}
              </p>
              <div className="flex flex-col sm:flex-row gap-4 justify-center">
                <a href="mailto:contact@gauratechnologies.com">
                  <Button size="lg" className="px-8">
                    {t("contactUs")}
                    <ArrowRight className="ml-2 h-4 w-4" />
                  </Button>
                </a>
                <Link href="/">
                  <Button size="lg" variant="outline" className="px-8">
                    {t("backToHome")}
                  </Button>
                </Link>
              </div>
            </div>
          </div>
        </section>
      </main>
      <Footer />
    </div>
  );
}
