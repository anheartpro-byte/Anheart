# Exceptions de l'audit des dépendances

Le job `audit` de la CI, vérification obligatoire sur `develop` comme sur
`main`, échoue quand :

- `npm audit` trouve un avis de gravité **haute ou critique** dans les paquets
  de `package-lock.json` (site et Convex). Un avis modéré ou faible est
  affiché et ne fait pas échouer ;
- `pip-audit` trouve une vulnérabilité connue, **quelle que soit sa gravité**
  (il ne lit pas la gravité), dans l'un des environnements Python audités :
  l'image du Pi, l'environnement des tests et de la simulation, la simulation
  hébergée, l'extracteur CAO.

Ce qui est audité, et comment : [framework-de-test.md](../docs/framework-de-test.md#dépendances--un-gestionnaire-des-versions-figées-un-audit-anh-153).

Une exception laisse passer **un avis précis, pour un temps borné**, quand
aucune version corrigée ne peut être prise tout de suite. C'est une ligne du
tableau ci-dessous, ajoutée par une PR relue comme une autre. Le job lit ce
fichier à chaque exécution (`scripts/ci/audit-exceptions.mjs`) ; aucune
exception n'est écrite ailleurs, ni dans le workflow ni dans une commande.

## Règles

- **Une ligne par avis et par écosystème.** `Identifiant` : pour npm, l'avis
  GitHub `GHSA-xxxx-xxxx-xxxx`, tel qu'il termine l'adresse que donne
  `npm audit` ; pour pip, l'identifiant qu'affiche `pip-audit`, `PYSEC-...` ou
  `GHSA-...`. Un `CVE-...` est refusé pour pip (voir le point suivant).
- `Écosystème` : `npm` ou `pip`. `Paquet` : le paquet que l'avis touche.
  - **npm** : l'exception ne vaut que pour cet avis, dans ce paquet. Le même
    avis dans un autre paquet, ou un autre avis du même paquet, fait encore
    échouer le job.
  - **pip** : l'exception n'est liée ni au paquet ni à l'environnement.
    `pip-audit` reçoit l'identifiant par `--ignore-vuln` et saute toute
    vulnérabilité dont l'identifiant, **ou l'un des alias**, est celui-là,
    quel que soit le paquet, dans chacun des cinq environnements audités. La
    colonne `Paquet` ne restreint rien : elle dit à un lecteur de quoi il
    s'agit. C'est pourquoi un `CVE-...` est refusé : le même CVE peut être
    l'alias d'avis de plusieurs paquets, quand une bibliothèque vulnérable est
    livrée par plus d'un.
- `Ajoutée le` et `Expire le` : dates `AAAA-MM-JJ`, **90 jours au plus** de
  l'une à l'autre. **Dès le jour d'expiration, le job `audit` échoue**, sur
  toute PR, et sur l'exécution de nuit une fois la ligne arrivée sur `main`,
  jusqu'à ce qu'une PR retire la ligne ou la renouvelle en disant pourquoi.
- `Ticket` : le ticket Linear qui retirera l'exception, en lien
  `[ANH-n](https://linear.app/anheart/issue/ANH-n/...)`.
- `Raison` : pourquoi l'avis ne touche pas Anheart, ou pourquoi il est accepté
  jusque-là. Pas de barre verticale dans le texte.

**Le tableau est le seul de la section**, ses lignes à la suite les unes des
autres, sans retrait. Sont refusés : une ligne qui commence par une barre
verticale ailleurs dans la section, un bloc de code dans la section, un second
titre « Exceptions en cours » ou un titre écrit dans un bloc de code, et,
n'importe où dans ce fichier, code compris, le signe `<` immédiatement suivi
d'une lettre Unicode, de `/`, de `!` ou de `?` : c'est ainsi que s'ouvre toute
balise ou tout bloc HTML, y compris un bloc `details` replié au milieu d'une
ligne, un commentaire, une déclaration, une section CDATA ou une instruction
de traitement ; un lien entre chevrons qui commence par une lettre, comme une
adresse web, l'est aussi. Est également refusée toute ligne qui commence par
`<` après trois espaces au plus. Ailleurs qu'en début de ligne, tout autre `<`
reste permis, par exemple suivi d'une espace, d'un chiffre ou de `=`. Ce que
la page affichée ne montre pas ne peut donc pas compter comme exception.

Une exception npm ne cache pas l'avis : le job l'affiche encore, avec sa date
d'expiration et son ticket. `pip-audit` compte les vulnérabilités sautées
(« N ignored »). Une ligne mal formée, ou expirée, fait échouer le
job, et aucun avis n'est alors sauté : rien ne l'est sur la foi d'un tableau qui
ne suit pas ces règles.

Pour vérifier le tableau avant de pousser :

```sh
node scripts/ci/audit-exceptions.mjs check
```

## Exceptions en cours

| Identifiant | Écosystème | Paquet | Ajoutée le | Expire le | Ticket | Raison |
|---|---|---|---|---|---|---|

Aucune exception au 14 octobre 2026 : `npm audit` et `pip-audit` ne trouvent
rien à exempter.

## Ce qui n'est pas une exception

`package.json` remplace la dépendance transitive `braces` par une version qui
corrige l'avis
[GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)
(champ `overrides`). Ce remplacement ne fait sauter aucun avis à l'audit :
`npm audit` juge la version installée, et des tests de régression tournent dans
le même job. Le choix et ses limites sont décrits dans
[suivi-tickets.md](../docs/suivi-tickets.md#dépendance-de-lint--correctif-ciblé-et-audit-maintenu).
