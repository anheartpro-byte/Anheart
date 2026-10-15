export const CONSENT_TEXT_VERSION = "0.1.0";
export const CONSENT_TEXT_PROVISIONAL = true;

export const CONSENT_TEXT: string = [
  "Une séance utilise une centrifugeuse en rotation. La fréquence cardiaque du passager est mesurée et pilote la vitesse de rotation.",
  "L’opérateur peut arrêter la séance à tout moment. Le passager peut aussi demander son arrêt à tout moment.",
  "La séance enregistre la fréquence cardiaque, l’ECG et la télémétrie de séance. Le passager, ses gestionnaires et les administrateurs peuvent consulter ces données. Les réponses au questionnaire d’aptitude sont visibles uniquement par ses gestionnaires et les administrateurs.",
  "Le passager peut retirer son consentement à tout moment depuis le site. Après ce retrait, aucun nouveau lancement de séance n’est possible.",
  "La durée de conservation de ces données sera précisée. Ce texte est provisoire et ne constitue pas un avis juridique.",
].join("\n\n");

export function consentParagraphs(text = CONSENT_TEXT): string[] {
  return text
    .split(/\r?\n\s*\r?\n/)
    .map((paragraph) => paragraph.trim())
    .filter((paragraph) => paragraph.length > 0);
}
