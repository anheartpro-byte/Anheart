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
          question: "What is AnHeart?",
          answer:
            "AnHeart is a revolutionary cardiovascular training system that uses centrifugal force to create artificial gravity. This technology enables effective heart training, recovery improvement, and various medical rehabilitation applications.",
        },
        {
          question: "How does artificial gravity training work?",
          answer:
            "The AnHeart machine rotates to create centrifugal force, which pushes blood towards the lower extremities. This simulates gravitational effects and forces the heart to work harder, providing effective cardiovascular training similar to standing or exercising, even while lying down.",
        },
        {
          question: "Is AnHeart technology scientifically proven?",
          answer:
            "Yes, AnHeart technology is backed by over 40 years of research and more than 30 published scientific papers. Clinical studies have shown significant improvements in vascularization, mobility, and cardiovascular health.",
        },
        {
          question: "Where is AnHeart based?",
          answer:
            "AnHeart is headquartered in Paris, France. We serve clients across Europe and the United States, with expansion plans for global markets.",
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
            "During AnHeart training, heart rates can range from 40-160 BPM, compared to 40-60 BPM lying down and 60-80 BPM standing up. This wide range allows for highly customizable and effective cardiovascular training.",
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
      category: "Health Benefits",
      faqs: [
        {
          question: "What are the main health benefits?",
          answer:
            "AnHeart training provides numerous benefits including: heart training and strengthening, improved recovery from injuries, enhanced VO2 Max, better leg vascularization, injury prevention, maintained muscle and bone mass, improved balance, and enhanced cardiovascular health.",
        },
        {
          question: "Can AnHeart help with lymphedema?",
          answer:
            "Yes, clinical studies have shown significant improvement in patients with lymphedema. For example, a 48-year-old female patient with severe lymphedema showed remarkable improvement after just 10 GT sessions.",
        },
        {
          question: "What mobility improvements can be expected?",
          answer:
            "After 45 GT sessions, clinical trials have shown walking mobility improvement of up to 15x and cycling mobility improvement of up to 7x. Results vary based on individual conditions and training consistency.",
        },
        {
          question: "Is AnHeart suitable for elderly patients?",
          answer:
            "Yes, AnHeart is particularly beneficial for elderly patients, including those with osteoporosis. The low-impact nature of the training allows for effective cardiovascular exercise without the stress of traditional weight-bearing activities.",
        },
      ],
    },
    {
      category: "Medical Applications",
      faqs: [
        {
          question: "What medical conditions can benefit from AnHeart?",
          answer:
            "AnHeart technology is beneficial for various medical conditions including: quadriplegia and wheelchair users, overweight individuals, elderly patients with osteoporosis, cardiac rehabilitation programs, and post-surgery recovery.",
        },
        {
          question: "Is AnHeart used in professional medical settings?",
          answer:
            "Yes, AnHeart is used in over 27,000 medical centers including Inpatient Rehabilitation Facilities (IRF), Comprehensive Rehabilitation clinics, and specialized rehab centers across the US and Europe.",
        },
        {
          question: "Do I need a doctor's prescription to use AnHeart?",
          answer:
            "For medical rehabilitation applications, we recommend consulting with a healthcare provider. For athletic performance training, sessions are typically conducted under the supervision of trained professionals.",
        },
        {
          question: "How is patient safety ensured during sessions?",
          answer:
            "Safety is ensured through real-time ECG monitoring, trained supervision, personalized training protocols, and automatic safety controls. All sessions are monitored for heart rate, rhythm, and other vital parameters.",
        },
      ],
    },
    {
      category: "Athletes & Performance",
      faqs: [
        {
          question: "Which professional sports teams use AnHeart?",
          answer:
            "AnHeart technology is used by over 150 professional clubs across NBA, NFL, FIFA, and other major sports leagues. Athletes use it for performance training, injury recovery, and maintaining cardiovascular fitness.",
        },
        {
          question: "How can athletes benefit from AnHeart training?",
          answer:
            "Athletes benefit through improved cardiovascular endurance, faster recovery from injuries, enhanced VO2 Max, better blood circulation, maintained fitness during injury rehabilitation, and reduced risk of future injuries.",
        },
        {
          question: "Can AnHeart be used during injury recovery?",
          answer:
            "Yes, AnHeart is particularly valuable during injury recovery as it allows athletes to maintain cardiovascular fitness and continue heart training without putting stress on injured body parts.",
        },
        {
          question: "What is the typical training schedule for athletes?",
          answer:
            "Professional athletes typically use AnHeart for 30-40 minutes daily. The intensity and frequency can be adjusted based on training goals, competition schedules, and recovery needs.",
        },
      ],
    },
  ],
  fr: [
    {
      category: "Questions Générales",
      faqs: [
        {
          question: "Qu'est-ce qu'AnHeart?",
          answer:
            "AnHeart est un système révolutionnaire d'entraînement cardiovasculaire qui utilise la force centrifuge pour créer une gravité artificielle. Cette technologie permet un entraînement cardiaque efficace, une amélioration de la récupération et diverses applications de rééducation médicale.",
        },
        {
          question:
            "Comment fonctionne l'entraînement par gravité artificielle?",
          answer:
            "La machine AnHeart tourne pour créer une force centrifuge, qui pousse le sang vers les extrémités inférieures. Cela simule les effets gravitationnels et force le cœur à travailler plus dur, offrant un entraînement cardiovasculaire efficace similaire à la position debout ou à l'exercice, même en position allongée.",
        },
        {
          question: "La technologie AnHeart est-elle scientifiquement prouvée?",
          answer:
            "Oui, la technologie AnHeart est soutenue par plus de 40 ans de recherche et plus de 30 articles scientifiques publiés. Les études cliniques ont montré des améliorations significatives de la vascularisation, de la mobilité et de la santé cardiovasculaire.",
        },
        {
          question: "Où est basé AnHeart?",
          answer:
            "AnHeart a son siège à Paris, en France. Nous servons des clients à travers l'Europe et les États-Unis, avec des plans d'expansion vers les marchés mondiaux.",
        },
      ],
    },
    {
      category: "Technologie et Entraînement",
      faqs: [
        {
          question: "Quelle est la durée d'entraînement recommandée?",
          answer:
            "Le protocole d'entraînement recommandé est de 30 à 40 minutes par jour à 1-3g (fois la gravité terrestre). La durée et l'intensité exactes sont personnalisées en fonction des conditions de santé individuelles et des objectifs d'entraînement.",
        },
        {
          question:
            "Quelle fréquence cardiaque peut être atteinte pendant l'entraînement?",
          answer:
            "Pendant l'entraînement AnHeart, les fréquences cardiaques peuvent varier de 40 à 160 BPM, contre 40-60 BPM en position allongée et 60-80 BPM debout. Cette large gamme permet un entraînement cardiovasculaire hautement personnalisable et efficace.",
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
      category: "Bienfaits pour la Santé",
      faqs: [
        {
          question: "Quels sont les principaux bienfaits pour la santé?",
          answer:
            "L'entraînement AnHeart offre de nombreux avantages: entraînement et renforcement cardiaque, récupération améliorée après les blessures, VO2 Max amélioré, meilleure vascularisation des jambes, prévention des blessures, maintien de la masse musculaire et osseuse, amélioration de l'équilibre et santé cardiovasculaire renforcée.",
        },
        {
          question: "AnHeart peut-il aider en cas de lymphœdème?",
          answer:
            "Oui, les études cliniques ont montré une amélioration significative chez les patients atteints de lymphœdème. Par exemple, une patiente de 48 ans atteinte d'un lymphœdème sévère a montré une amélioration remarquable après seulement 10 séances GT.",
        },
        {
          question: "Quelles améliorations de mobilité peut-on attendre?",
          answer:
            "Après 45 séances GT, les essais cliniques ont montré une amélioration de la mobilité de marche jusqu'à 15 fois et une amélioration de la mobilité à vélo jusqu'à 7 fois. Les résultats varient en fonction des conditions individuelles et de la régularité de l'entraînement.",
        },
        {
          question: "AnHeart convient-il aux patients âgés?",
          answer:
            "Oui, AnHeart est particulièrement bénéfique pour les patients âgés, y compris ceux souffrant d'ostéoporose. La nature à faible impact de l'entraînement permet un exercice cardiovasculaire efficace sans le stress des activités traditionnelles avec mise en charge.",
        },
      ],
    },
    {
      category: "Applications Médicales",
      faqs: [
        {
          question:
            "Quelles conditions médicales peuvent bénéficier d'AnHeart?",
          answer:
            "La technologie AnHeart est bénéfique pour diverses conditions médicales: tétraplégiques et utilisateurs de fauteuils roulants, personnes en surpoids, patients âgés atteints d'ostéoporose, programmes de réhabilitation cardiaque et récupération post-chirurgicale.",
        },
        {
          question:
            "AnHeart est-il utilisé dans des contextes médicaux professionnels?",
          answer:
            "Oui, AnHeart est utilisé dans plus de 27 000 centres médicaux, y compris les établissements de réadaptation pour patients hospitalisés (IRF), les cliniques de réadaptation complète et les centres de rééducation spécialisés à travers les États-Unis et l'Europe.",
        },
        {
          question:
            "Ai-je besoin d'une ordonnance médicale pour utiliser AnHeart?",
          answer:
            "Pour les applications de rééducation médicale, nous recommandons de consulter un professionnel de santé. Pour l'entraînement de performance sportive, les sessions sont généralement effectuées sous la supervision de professionnels formés.",
        },
        {
          question:
            "Comment la sécurité des patients est-elle assurée pendant les sessions?",
          answer:
            "La sécurité est assurée par une surveillance ECG en temps réel, une supervision formée, des protocoles d'entraînement personnalisés et des contrôles de sécurité automatiques. Toutes les sessions sont surveillées pour la fréquence cardiaque, le rythme et d'autres paramètres vitaux.",
        },
      ],
    },
    {
      category: "Athlètes et Performance",
      faqs: [
        {
          question:
            "Quelles équipes sportives professionnelles utilisent AnHeart?",
          answer:
            "La technologie AnHeart est utilisée par plus de 150 clubs professionnels à travers la NBA, la NFL, la FIFA et d'autres ligues sportives majeures. Les athlètes l'utilisent pour l'entraînement de performance, la récupération après blessure et le maintien de la forme cardiovasculaire.",
        },
        {
          question:
            "Comment les athlètes peuvent-ils bénéficier de l'entraînement AnHeart?",
          answer:
            "Les athlètes bénéficient d'une endurance cardiovasculaire améliorée, d'une récupération plus rapide après les blessures, d'un VO2 Max amélioré, d'une meilleure circulation sanguine, d'un maintien de la forme pendant la rééducation après blessure et d'un risque réduit de blessures futures.",
        },
        {
          question:
            "AnHeart peut-il être utilisé pendant la récupération après une blessure?",
          answer:
            "Oui, AnHeart est particulièrement précieux pendant la récupération après une blessure car il permet aux athlètes de maintenir leur forme cardiovasculaire et de continuer l'entraînement cardiaque sans solliciter les parties du corps blessées.",
        },
        {
          question:
            "Quel est le programme d'entraînement typique pour les athlètes?",
          answer:
            "Les athlètes professionnels utilisent généralement AnHeart pendant 30 à 40 minutes par jour. L'intensité et la fréquence peuvent être ajustées en fonction des objectifs d'entraînement, des calendriers de compétition et des besoins de récupération.",
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
            <div className="max-w-3xl mx-auto text-center">
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
                  <h2 className="text-2xl font-semibold mb-6">
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
                <a href="mailto:contact@anheart.com">
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
