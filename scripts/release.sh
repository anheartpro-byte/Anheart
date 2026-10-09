#!/usr/bin/env bash
# ANH-134 : release des trois composants (pi, cloud, web).
# Mode d'emploi, règles et check-list : docs/release.md.
#
# Trois étapes, chacune avec --dry-run (lit tout, n'écrit rien) :
#   prepare  écrit les fichiers de version et CHANGELOG.md sur une branche
#            release/..., et ouvre la PR de préparation vers develop ;
#   pr       ouvre la PR develop -> main avec le modèle de release rempli ;
#   tag      pose les tags annotés sur main et les pousse.
#
# Règle tenue par pr et tag : aucun tag n'est posé sur un commit qui contient
# une PR de ticket d'un composant absente de la section de sa version. Si
# develop a bougé depuis prepare, ils refusent, et prepare relancé avec les
# mêmes versions complète les sections.
#
# Règle tenue par les trois étapes : un revert porte un titre de ticket. Un
# revert fusionné sous le titre que GitHub propose (Revert "...") fait refuser
# tant qu'il n'est pas lui-même annulé (voir untitled_reverts).
#
# Compatible bash 3.2 (macOS) et bash 5 (Linux), awk de BSD, mawk et gawk : ni
# tableau associatif, ni mapfile, ni extension de gawk.
set -euo pipefail

REMOTE=origin
DEVELOP="${RELEASE_DEVELOP:-develop}"
MAIN="${RELEASE_MAIN:-main}"
COMPONENTS="pi cloud web"
# Les jobs de .github/workflows/ci.yml sans lesquels un commit n'est pas vert.
# Si un job est renommé, cette liste doit suivre : sinon le script refuse tout.
REQUIRED_CHECKS="pi-gate simulation-gate convex-tests web audit docs"
# Les statuts de commit sans lesquels une release ne part pas. Ce ne sont pas
# des jobs : l'avis d'agent indépendant est posé comme statut sur le SHA relu.
# La protection de main et celle de develop exigent le même nom.
REQUIRED_STATUSES="agent-review/R1"
MARKER='<!-- release.sh : nouvelles sections sous cette ligne -->'
VERSIONS_SLOT='<!-- release.sh : versions -->'
CHANGELOG_SLOT='<!-- release.sh : changelog -->'
TEMPLATE='.github/PULL_REQUEST_TEMPLATE/release.md'
SEMVER='(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'

DRY_RUN=0
NEW_pi=""
NEW_cloud=""
NEW_web=""
PI_LEVEL=""
RELEASE_DATE=""
TMP=""
CI_SUMMARY=""
# Les statuts du dernier commit jugé par require_green.
COMMIT_STATUSES=""
# Ce que la pipeline de release (.github/workflows/release.yml) dit à tag, et
# elle seule : le commit qu'elle a testé, et le numéro de sa propre exécution.
EXPECT_COMMIT=""
PIPELINE_RUN=""
# Ce que prepare doit défaire s'il échoue après avoir changé de branche.
PREP_BRANCH=""
PREP_BACK=""
PREP_PUSHED=0

usage() {
  cat <<'EOF'
Usage :
  scripts/release.sh prepare [--pi X.Y.Z] [--cloud X.Y.Z] [--web X.Y.Z]
                             [--pi-validation bench|auto_validated|occupied_validated]
                             [--date AAAA-MM-JJ] [--dry-run]
  scripts/release.sh pr  [--dry-run]
  scripts/release.sh tag [--dry-run] [--expect-commit SHA] [--pipeline-run ID]

prepare  depuis origin/develop vert : fichiers de version, CHANGELOG.md, PR vers develop.
         Relancé avec les mêmes versions quand develop a bougé, il complète les sections.
pr       après fusion de la préparation : PR develop -> main (modèle de release).
tag      après fusion dans main : tags annotés pi-X.Y.Z, cloud-X.Y.Z, web-X.Y.Z.

pr et tag refusent si le commit à publier contient une PR de ticket d'un
composant que la section de sa version ne cite pas. Les trois étapes refusent
un revert fusionné sans titre de ticket (Revert "..."), et un commit dont un
check run ou un statut n'est pas réussi. tag exige en plus l'avis indépendant
(statut agent-review/R1) sur le commit de develop que main a reçu.

--expect-commit et --pipeline-run servent à la pipeline de release, qui lance
tag depuis GitHub Actions. Avec le premier, tag refuse si main n'est plus au
commit donné. Avec le second, les vérifications de l'exécution donnée, celle
qui lance tag, ne sont pas jugées : ses étapes précédentes ont réussi avant
que tag démarre, et son propre job est en cours. Toutes les autres le restent.

prepare qui échoue après avoir changé de branche rend le clone tel qu'il
était ; relancé après un push réussi sans PR, il crée seulement la PR.

--dry-run affiche exactement ce qui serait fait, sans rien écrire, pousser ni créer.
Voir docs/release.md.
EOF
}

die() {
  printf 'release.sh : %s\n' "$*" >&2
  exit 1
}

label() {
  case "$1" in
    pi) printf 'Raspberry Pi' ;;
    cloud) printf 'Convex' ;;
    web) printf 'Site' ;;
  esac
}

# Version d'un composant dans une référence git, sans préfixe ; vide si absente.
current_version() { # composant référence
  local raw=""
  case "$1" in
    pi)
      raw="$(git show "$2:raspberry-pi/VERSION" 2>/dev/null || true)"
      raw="${raw#pi-}"
      ;;
    cloud)
      raw="$(git show "$2:convex/VERSION" 2>/dev/null || true)"
      raw="${raw#cloud-}"
      ;;
    web)
      raw="$(git show "$2:package.json" 2>/dev/null |
        sed -n 's/^  "version": "\([^"]*\)",\{0,1\}$/\1/p' | head -n 1 || true)"
      ;;
  esac
  printf '%s' "$raw"
}

# Vrai si $1 (X.Y.Z) est strictement supérieure à $2 (X.Y.Z ou X.Y.Z-dev).
version_gt() {
  local n1 n2 n3 c1 c2 c3 core="${2%-dev}"
  IFS=. read -r n1 n2 n3 <<EOF
$1
EOF
  IFS=. read -r c1 c2 c3 <<EOF
$core
EOF
  if [ "$n1" -ne "$c1" ]; then [ "$n1" -gt "$c1" ]; return; fi
  if [ "$n2" -ne "$c2" ]; then [ "$n2" -gt "$c2" ]; return; fi
  if [ "$n3" -ne "$c3" ]; then [ "$n3" -gt "$c3" ]; return; fi
  [ "$core" != "$2" ]
}

tag_exists() {
  git rev-parse -q --verify "refs/tags/$1" >/dev/null 2>&1
}

sha_of() {
  git rev-parse -q --verify "$1^{commit}" || die "référence introuvable : $1"
}

# Le tag de la version précédente d'un composant dont $2 est la version à
# publier : son tag le plus récent, qui doit lui être inférieur. Vide pour une
# première version. C'est de là que part le changelog de la version.
base_tag() { # composant version
  local t last=""
  for t in $(git tag -l "$1-[0-9]*" --sort=-v:refname); do
    if matches "$t" "^$1-$SEMVER\$"; then
      last="$t"
      break
    fi
  done
  if [ -n "$last" ] && ! version_gt "$2" "${last#$1-}"; then
    die "le tag $last existe et n'est pas inférieur à $1-$2"
  fi
  printf '%s' "$last"
}

# Le commit le plus récent de la chaîne de develop que contient le commit $1.
develop_tip_in() { # commit
  local h
  for h in $(git rev-list --first-parent "$REMOTE/$DEVELOP"); do
    if git merge-base --is-ancestor "$h" "$1"; then
      printf '%s' "$h"
      return 0
    fi
  done
  return 1
}

# Vrai si la chaîne entière $1 correspond à l'expression régulière étendue $2.
matches() {
  [[ "$1" =~ $2 ]]
}

is_level() {
  case "$1" in bench | auto_validated | occupied_validated) return 0 ;; esac
  return 1
}

# Refuse si le commit n'est pas vert : aucun de ses check runs ne doit avoir
# échoué ni être en cours, et chaque gate de REQUIRED_CHECKS doit y avoir au
# moins une exécution terminée et réussie. Un check run tiers réussi (les
# commentaires d'aperçu de Vercel, par exemple) ne remplace jamais une gate :
# un commit sur lequel la CI n'a pas tourné n'est pas vert.
#
# Une gate ignorée (skipped) ne compte ni pour ni contre. La CI n'en ignore que
# sur une PR, jamais sur un push vers develop ou main : la tête de ces branches
# porte donc une exécution réussie de chaque gate, même si la PR de release,
# ouverte sur ce même commit, en a ignoré une. Une gate qui n'a que des
# exécutions ignorées est refusée.
#
# Les jobs des deux boutons de déploiement Vercel (ANH-198) sont des check runs
# comme les autres : une exécution de bouton en échec, annulée, en cours ou en
# attente d'approbation sur ce commit fait refuser (docs/release.md, section 4,
# « Les boutons de déploiement et le script »).
#
# Les statuts de commit sont lus aussi (ANH-195). GitHub garde le dernier de
# chaque nom, et un statut qui n'est pas « success » (en échec, en erreur ou en
# attente) fait refuser, comme un check run. Un commit qui ne porte aucun
# statut n'est pas refusé ici : l'avis indépendant, lui, est exigé par
# require_review, sur le commit qui a été relu.
#
# Lancé par la pipeline de release (--pipeline-run), tag tourne dans un job de
# GitHub Actions, qui est lui-même un check run du commit, en cours tant que le
# script tourne. Les check runs de cette exécution-là ne sont donc pas jugés :
# ses étapes précédentes ont dû réussir pour que ce job démarre, et les
# suivantes n'ont pas commencé. Ils ne comptent pas non plus pour une gate : la
# pipeline ne se porte pas garante d'elle-même, et chaque gate doit avoir
# réussi dans une autre exécution, celle du push sur main. Une exécution se
# reconnaît à l'adresse de ses check runs (.../actions/runs/<numéro>/job/...).
require_green() { # sha branche
  local runs statuses bad missing name total all own=""
  runs="$(gh api "repos/{owner}/{repo}/commits/$1/check-runs" --paginate \
    --jq '.check_runs[] | [.name, .status, (.conclusion // ""), (.details_url // "")] | @tsv')" ||
    die "lecture des vérifications CI impossible pour $2 ($1)"
  [ -n "$runs" ] || die "aucune vérification CI pour $2 ($1) : attendre la CI"
  if [ -n "$PIPELINE_RUN" ]; then
    all="$(printf '%s\n' "$runs" | wc -l | tr -d ' ')"
    runs="$(printf '%s\n' "$runs" |
      awk -F '\t' -v own="/actions/runs/$PIPELINE_RUN/job/" 'index($4, own) == 0')"
    [ -n "$runs" ] ||
      die "$2 ($1) n'a pas d'autre vérification que celles de l'exécution $PIPELINE_RUN : chaque gate doit avoir réussi dans une autre exécution, celle du push sur $2"
    total="$(printf '%s\n' "$runs" | wc -l | tr -d ' ')"
    own=", sans juger les $((all - total)) vérifications de l'exécution $PIPELINE_RUN qui lance cette étape"
  fi
  statuses="$(commit_statuses "$1")" ||
    die "lecture des statuts de commit impossible pour $2 ($1)"
  COMMIT_STATUSES="$statuses"
  bad="$(
    printf '%s\n' "$runs" | awk -F '\t' '
      $2 != "completed" || ($3 != "success" && $3 != "skipped" && $3 != "neutral") {
        printf "  - %s : %s %s\n", $1, $2, $3
      }'
    printf '%s\n' "$statuses" | awk -F '\t' '
      $1 != "" && $2 != "success" { printf "  - statut %s : %s\n", $1, $2 }'
  )"
  [ -z "$bad" ] || die "$2 n'est pas vert ($1) :
$bad"
  missing=""
  for name in $REQUIRED_CHECKS; do
    printf '%s\n' "$runs" | awk -F '\t' -v gate="$name" '
      $1 == gate && $2 == "completed" && $3 == "success" { ok = 1 }
      END { exit !ok }' || missing="$missing $name"
  done
  [ -z "$missing" ] ||
    die "$2 n'est pas vert ($1) : gates absentes ou non réussies :$missing"
  total="$(printf '%s\n' "$runs" | wc -l | tr -d ' ')"
  CI_SUMMARY="verte ($total vérifications terminées, toutes les gates réussies$own)"
}

# Les statuts d'un commit, le dernier de chaque nom : lignes "nom<TAB>état"
# (success, failure, error ou pending). Rien si le commit n'en porte aucun.
commit_statuses() { # sha
  gh api "repos/{owner}/{repo}/commits/$1/status" --paginate \
    --jq '.statuses[] | [.context, .state] | @tsv'
}

# L'état d'un statut dans la sortie de commit_statuses ; « absent » sinon.
status_state() { # nom ; statuts sur l'entrée standard
  awk -F '\t' -v name="$1" '
    $1 == name { state = $2 }
    END { print (state == "" ? "absent" : state) }'
}

# Refuse si le commit relu ne porte pas, réussi, chaque statut de
# REQUIRED_STATUSES. La release ne part pas sans l'avis indépendant : tag le
# vérifie sur le commit de develop que main a reçu, celui que la PR de release
# portait en tête et que la protection de main a jugé.
require_review() { # sha
  local statuses name state missing=""
  statuses="$(commit_statuses "$1")" ||
    die "lecture des statuts de commit impossible pour $1"
  for name in $REQUIRED_STATUSES; do
    state="$(printf '%s\n' "$statuses" | status_state "$name")"
    [ "$state" = success ] || missing="$missing
  - $name : $state"
  done
  [ -z "$missing" ] ||
    die "le candidat publié ($1) n'a pas l'avis indépendant requis :$missing
La release ne part pas sans lui : faire relire ce commit, faire poser le statut, puis relancer."
}

# L'état de chaque statut requis, pour l'afficher : "nom : état".
review_summary() { # statuts (sortie de commit_statuses) sur l'entrée standard
  local statuses name out=""
  statuses="$(cat)"
  for name in $REQUIRED_STATUSES; do
    out="$out${out:+, }$name : $(printf '%s\n' "$statuses" | status_state "$name")"
  done
  printf '%s' "$out"
}

# Titres des PR de ticket (ANH-n) qui touchent le composant, du plus ancien au
# plus récent, lus sur le premier parent de l'intervalle.
entries() { # composant intervalle
  git -c core.quotePath=false log --first-parent --reverse \
    --diff-merges=first-parent --name-only --format='@@%s' "$2" -- |
    awk -v comp="$1" '
      function touches(path) {
        if (comp == "pi") return path ~ /^raspberry-pi\//
        if (comp == "cloud") return path ~ /^convex\//
        return path ~ /^(app|components|hooks|i18n|lib|messages|public)\// ||
          path ~ /^(proxy\.ts|next\.config\.ts|package\.json|package-lock\.json|postcss\.config\.mjs|components\.json|tsconfig\.json)$/
      }
      function flush() { if (title != "" && hit) print "- " title }
      /^@@/ {
        flush()
        title = ""
        hit = 0
        subject = substr($0, 3)
        if (match(subject, /^ANH-[0-9]+ ?: */)) {
          id = subject
          sub(/ ?:.*$/, "", id)
          title = id " : " substr(subject, RLENGTH + 1)
        }
        next
      }
      title != "" && touches($0) { hit = 1 }
      END { flush() }'
}

# Les reverts sans titre de ticket qui touchent le composant dans l'intervalle :
# lignes "  <sha court> <titre>", du plus ancien au plus récent.
#
# Un revert porte un titre de ticket (ANH-n : ...), comme toute PR : il a alors
# sa ligne dans la section, à côté de celle du ticket qu'il annule. Fusionné
# sous le titre que GitHub propose (Revert "..."), il n'en aurait aucune : le
# changement annulé partirait, et la section citerait toujours son ticket. Les
# trois étapes refusent donc un tel revert.
#
# Un commit fusionné ne change plus de titre. Ce qui sort du refus : annuler ce
# revert par le bouton « Revert » de GitHub en gardant le titre proposé
# (Revert "Revert "..."", ou Reapply "..." écrit par git), puis refaire
# l'annulation sous un titre de ticket. Un revert au titre par défaut que son
# propre revert au titre par défaut annule, plus loin dans l'intervalle, ne
# compte plus : les deux se neutralisent, et rien n'est parti sans ligne.
untitled_reverts() { # composant intervalle
  git -c core.quotePath=false log --first-parent --reverse \
    --diff-merges=first-parent --name-only --format='@@%h %s' "$2" -- |
    awk -v comp="$1" '
      function touches(path) {
        if (comp == "pi") return path ~ /^raspberry-pi\//
        if (comp == "cloud") return path ~ /^convex\//
        return path ~ /^(app|components|hooks|i18n|lib|messages|public)\// ||
          path ~ /^(proxy\.ts|next\.config\.ts|package\.json|package-lock\.json|postcss\.config\.mjs|components\.json|tsconfig\.json)$/
      }
      # Range le revert en cours : il annule un revert encore ouvert, qui sort
      # alors de la liste avec lui, ou il en ouvre un.
      function flush(   i) {
        if (bare == "") return
        for (i = n; i >= 1; i--) {
          if (shown[i] != "" && (quoted == full[i] || quoted == stripped[i])) {
            shown[i] = ""
            return
          }
        }
        n++
        shown[n] = line
        full[n] = subject
        stripped[n] = bare
        hits[n] = hit
      }
      /^@@/ {
        flush()
        bare = ""
        hit = 0
        line = substr($0, 3)
        subject = line
        sub(/^[^ ]* /, "", subject)
        text = subject
        # Le suffixe " (#12)" que pose une fusion squash ne fait pas partie du titre.
        sub(/ \(#[0-9]+\)$/, "", text)
        # git écrit Reapply "X" pour le revert de Revert "X".
        if (text ~ /^Reapply ".*"$/) text = "Revert \"Revert " substr(text, 9) "\""
        if (text ~ /^Revert ".*"$/) {
          bare = text
          quoted = substr(text, 9, length(text) - 9)
        }
        next
      }
      bare != "" && touches($0) { hit = 1 }
      END {
        flush()
        for (i = 1; i <= n; i++) if (shown[i] != "" && hits[i]) print "  " shown[i]
      }'
}

# Refuse si l'intervalle à publier pour un composant contient un revert sans
# titre de ticket.
require_titled_reverts() { # composant intervalle
  local found
  found="$(untitled_reverts "$1" "$2")" ||
    die "lecture de l'historique impossible pour $2"
  [ -z "$found" ] ||
    die "un revert sans titre de ticket touche $1 ($(label "$1")) dans $2 :
$found
Un revert porte un titre de ticket (ANH-n : ...) : sans lui il partirait sans ligne dans CHANGELOG.md, qui citerait toujours le ticket annulé.
Pour en sortir : annuler ce revert par le bouton « Revert » de GitHub en gardant le titre proposé, puis refaire l'annulation dans une PR intitulée ANH-n : ... (docs/release.md, section 3)."
}

# Section de CHANGELOG.md d'une version.
section() { # composant version date niveau base intervalle
  local list
  list="$(entries "$1" "$6")" || return 1
  printf '## %s-%s (%s)\n\n' "$1" "$2" "$3"
  printf 'Composant : %s. Changements depuis : %s.\n' "$(label "$1")" "$5"
  if [ "$1" = pi ]; then
    printf 'Niveau de validation : `%s`.\n' "$4"
  fi
  printf '\n%s\n' "${list:-- Aucune PR de ticket ne touche ce composant depuis $5.}"
}

# Section d'un tag, lue dans un CHANGELOG.md passé sur l'entrée standard.
extract_section() { # tag
  awk -v head="## $1 (" '
    index($0, head) == 1 { on = 1; print; next }
    on && /^## / { exit }
    on { print }'
}

level_of_section() {
  sed -n 's/^Niveau de validation : `\([a-z_]*\)`\.$/\1/p' | head -n 1
}

# Retire d'un CHANGELOG.md (entrée standard) les sections des tags donnés.
remove_sections() { # tags séparés par des espaces
  awk -v tags="$1" '
    BEGIN { n = split(tags, tag, " ") }
    /^## / {
      skip = 0
      for (i = 1; i <= n; i++) if (index($0, "## " tag[i] " (") == 1) skip = 1
    }
    !skip { print }'
}

# Insère les sections sous la ligne repère, une ligne vide avant les sections
# plus anciennes.
insert_sections() { # fichier_des_sections ; CHANGELOG.md sur l'entrée standard
  awk -v marker="$MARKER" -v file="$1" '
    after == 1 {
      if ($0 == "") next
      print ""
      after = 2
    }
    { print }
    $0 == marker && !found {
      found = 1
      after = 1
      print ""
      while ((getline line < file) > 0) print line
    }
    END { if (!found) exit 3 }'
}

fill_template() { # fichier_versions fichier_sections ; modèle sur l'entrée standard
  awk -v vslot="$VERSIONS_SLOT" -v cslot="$CHANGELOG_SLOT" -v vfile="$1" -v cfile="$2" '
    $0 == vslot { while ((getline line < vfile) > 0) print line; v = 1; next }
    $0 == cslot { while ((getline line < cfile) > 0) print line; c = 1; next }
    { print }
    END { if (!v || !c) exit 3 }'
}

versions_table() { # candidat avis ; lignes "composant version niveau" sur l'entrée standard
  local c v l
  printf '| Composant | Version | Niveau de validation |\n|---|---|---|\n'
  while read -r c v l; do
    if [ "$c" = pi ]; then
      printf '| %s | `%s-%s` | `%s` |\n' "$(label "$c")" "$c" "$v" "$l"
    else
      printf '| %s | `%s-%s` | sans objet |\n' "$(label "$c")" "$c" "$v"
    fi
  done
  printf '\nCandidat (`%s`) : %s\n' "$DEVELOP" "$1"
  printf '\nAvis indépendant sur ce commit : %s. `%s` exige ce statut réussi pour fusionner, et `scripts/release.sh tag` le vérifie avant de poser un tag.\n' "$2" "$MAIN"
}

cloud_constant() { # version
  cat <<EOF
/**
 * Version of the Convex backend. Written by \`scripts/release.sh\` together with
 * \`convex/VERSION\`: do not edit by hand. \`convex/cloudVersion.test.ts\` fails
 * if the two differ.
 */
export const CLOUD_VERSION = "cloud-$1";
EOF
}

set_package_version() { # version
  node - "$1" <<'JS' || die "écriture de la version dans package.json impossible"
const fs = require("node:fs");
const version = process.argv[2];
function patch(file, patterns) {
  let text = fs.readFileSync(file, "utf8");
  for (const pattern of patterns) {
    if (!pattern.test(text)) throw new Error(`${file} : champ version introuvable`);
    text = text.replace(pattern, `$1${version}$2`);
  }
  fs.writeFileSync(file, text);
  return JSON.parse(text);
}
const root = /^(  "version": ")[^"]*(")/m;
const pkg = patch("package.json", [root]);
const lock = patch("package-lock.json", [
  root,
  /^(    "": \{\n(?:      .*\n)*?      "version": ")[^"]*(")/m,
]);
if (
  pkg.version !== version ||
  lock.version !== version ||
  lock.packages[""].version !== version
) {
  throw new Error("version incohérente après écriture");
}
JS
}

heading() {
  printf '\n-- %s --\n' "$1"
}

start() { # étape
  command -v gh >/dev/null 2>&1 || die "la commande gh (GitHub CLI) est requise"
  git rev-parse --show-toplevel >/dev/null 2>&1 || die "à lancer dans le dépôt Anheart"
  cd "$(git rev-parse --show-toplevel)"
  if [ "$DRY_RUN" -eq 0 ] && { [ "$DEVELOP" != develop ] || [ "$MAIN" != main ]; }; then
    die "RELEASE_DEVELOP et RELEASE_MAIN ne servent qu'avec --dry-run"
  fi
  TMP="$(mktemp -d)"
  trap cleanup EXIT
  git fetch --quiet --tags "$REMOTE" || die "git fetch $REMOTE a échoué"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf "== release.sh %s : simulation, rien n'est écrit, poussé ni créé ==\n" "$1"
  else
    printf '== release.sh %s ==\n' "$1"
  fi
}

# Versions d'une référence qui n'ont pas encore de tag : lignes "composant version".
pending_versions() { # référence
  local c v
  for c in $COMPONENTS; do
    v="$(current_version "$c" "$1")"
    if matches "$v" "^$SEMVER\$" && ! tag_exists "$c-$v"; then
      printf '%s %s\n' "$c" "$v"
    fi
  done
}

# Écrit $TMP/sections.md et $TMP/released (lignes "composant version niveau") à
# partir du CHANGELOG.md de la référence, pour les versions sans tag.
collect_pending() { # référence
  local c v sec level
  git show "$1:CHANGELOG.md" >"$TMP/changelog.md" 2>/dev/null ||
    die "CHANGELOG.md absent de $1 : la release n'y est pas fusionnée"
  pending_versions "$1" >"$TMP/pending"
  [ -s "$TMP/pending" ] || die "rien à faire : aucune version de $1 n'attend son tag"
  : >"$TMP/sections.md"
  : >"$TMP/released"
  while read -r c v; do
    sec="$(extract_section "$c-$v" <"$TMP/changelog.md")"
    [ -n "$sec" ] || die "CHANGELOG.md de $1 n'a pas de section $c-$v : lancer prepare"
    level="-"
    if [ "$c" = pi ]; then
      level="$(printf '%s\n' "$sec" | level_of_section)"
      is_level "$level" || die "section pi-$v sans niveau de validation reconnu"
    fi
    [ ! -s "$TMP/sections.md" ] || printf '\n' >>"$TMP/sections.md"
    printf '%s\n' "$sec" >"$TMP/section-$c.md"
    printf '%s\n' "$sec" >>"$TMP/sections.md"
    printf '%s %s %s\n' "$c" "$v" "$level" >>"$TMP/released"
  done <"$TMP/pending"
}

# Refuse si le commit $1, que l'étape s'apprête à publier, contient sur la
# chaîne de develop une PR de ticket d'un composant que la section de sa version
# ne cite pas. C'est le cas dès qu'une PR est fusionnée dans develop après le
# calcul des sections par prepare : sans ce refus, elle serait livrée et taguée
# sans figurer dans aucun changelog, celui de la version suivante partant du tag.
require_complete_sections() { # commit suite_à_donner
  local c v base list line missing=""
  while read -r c v _; do
    base="$(base_tag "$c" "$v")" || exit 1
    require_titled_reverts "$c" "${base:+$base..}$1"
    list="$(entries "$c" "${base:+$base..}$1")" ||
      die "lecture de l'historique impossible jusqu'à $1"
    [ -n "$list" ] || continue
    while IFS= read -r line; do
      grep -Fxq -e "$line" "$TMP/section-$c.md" || missing="$missing
  $c-$v : ${line#- }"
    done <<EOF
$list
EOF
  done <"$TMP/released"
  [ -z "$missing" ] ||
    die "le commit à publier ($1) contient des PR de ticket que CHANGELOG.md ne cite pas :$missing
$DEVELOP a bougé depuis prepare. Relancer scripts/release.sh prepare avec les mêmes versions (il complète les sections), fusionner sa PR dans $DEVELOP, $2"
}

tags_of() { # fichier "composant version ..." ; séparateur
  awk -v sep="$2" '{ printf "%s%s-%s", (NR > 1 ? sep : ""), $1, $2 }' "$1"
}

release_pr_body() { # candidat avis
  versions_table "$1" "$2" <"$TMP/released" >"$TMP/versions.md"
  git show "$REMOTE/$DEVELOP:$TEMPLATE" >"$TMP/template.md" 2>/dev/null ||
    die "$TEMPLATE absent de $REMOTE/$DEVELOP"
  fill_template "$TMP/versions.md" "$TMP/sections.md" <"$TMP/template.md" >"$TMP/pr-release.md" ||
    die "$TEMPLATE n'a plus ses deux emplacements release.sh"
}

tag_message() { # composant version
  printf '%s-%s\n\n' "$1" "$2"
  sed '1,2d' "$TMP/section-$1.md"
}

# Écrit dans $TMP/out, sous leur chemin dans le dépôt, les fichiers que prepare
# validera : tout est calculé ici, avant de toucher au clone.
stage_prepared() { # référence ; fichiers de version (séparés par des espaces)
  local f
  mkdir -p "$TMP/out/raspberry-pi" "$TMP/out/convex"
  cp "$TMP/changelog.new" "$TMP/out/CHANGELOG.md"
  case " $2 " in *" raspberry-pi/VERSION "*)
    printf 'pi-%s\n' "$NEW_pi" >"$TMP/out/raspberry-pi/VERSION"
    ;;
  esac
  case " $2 " in *" convex/VERSION "*)
    printf 'cloud-%s\n' "$NEW_cloud" >"$TMP/out/convex/VERSION"
    cloud_constant "$NEW_cloud" >"$TMP/out/convex/cloudVersion.ts"
    ;;
  esac
  case " $2 " in *" package.json "*)
    for f in package.json package-lock.json; do
      git show "$1:$f" >"$TMP/out/$f" 2>/dev/null || die "$f absent de $1"
    done
    (cd "$TMP/out" && set_package_version "$NEW_web")
    ;;
  esac
}

# Vrai si le commit $1, tête d'une branche release/... déjà sur origin, est
# exactement celui que ce prepare écrirait : même parent, même titre, mêmes
# fichiers, même contenu. C'est le cas quand un prepare précédent a poussé sa
# branche puis n'a pas pu créer la PR.
same_preparation() { # commit parent titre fichiers
  local f wanted found
  [ "$(git rev-parse -q --verify "$1^" 2>/dev/null)" = "$2" ] || return 1
  [ "$(git log -1 --format=%s "$1")" = "$3" ] || return 1
  # shellcheck disable=SC2086
  wanted="$(printf '%s\n' CHANGELOG.md $4 | sort)"
  found="$(git diff --name-only "$2" "$1" | sort)"
  [ "$wanted" = "$found" ] || return 1
  for f in $wanted; do
    git show "$1:$f" 2>/dev/null | cmp -s - "$TMP/out/$f" || return 1
  done
}

# Crée la PR de préparation, sauf si elle est déjà ouverte pour cette branche.
open_prepare_pr() { # branche titre
  local url
  url="$(gh pr list --head "$1" --base "$DEVELOP" --state open --json url --jq '.[0].url // ""')" ||
    die "lecture des PR ouvertes impossible pour $1"
  if [ -n "$url" ]; then
    printf 'La PR de préparation est déjà ouverte : %s\n' "$url"
    return 0
  fi
  gh pr create --base "$DEVELOP" --head "$1" --title "$2" --body-file "$TMP/pr-prepare.md"
}

# À la sortie du script. Si prepare a échoué après avoir changé de branche, le
# clone est rendu tel qu'il était : l'arbre était sans modification avant le
# changement de branche, tout ce qu'il porte de plus vient donc de ce script.
cleanup() {
  local status=$?
  trap - EXIT
  if [ "$status" -ne 0 ] && [ -n "$PREP_BRANCH" ]; then
    git reset --quiet --hard >/dev/null 2>&1 || true
    # shellcheck disable=SC2086
    git switch --quiet $PREP_BACK >/dev/null 2>&1 || true
    git branch --quiet -D "$PREP_BRANCH" >/dev/null 2>&1 || true
    if [ "$PREP_PUSHED" -eq 1 ]; then
      printf 'release.sh : la branche %s est sur %s, mais sa PR n'"'"'a pas été créée. Le clone est rendu tel qu'"'"'il était. Relancer la même commande : elle reprend à la création de la PR.\n' "$PREP_BRANCH" "$REMOTE" >&2
    else
      printf 'release.sh : prepare a échoué après avoir créé la branche %s. Rien n'"'"'a été créé sur GitHub, et le clone est rendu tel qu'"'"'il était : corriger la cause, puis relancer la même commande.\n' "$PREP_BRANCH" >&2
    fi
  fi
  [ -z "$TMP" ] || rm -rf "$TMP"
  exit "$status"
}

cmd_prepare() {
  local c new cur ref sha base since range branch title files
  local today when was resumed changed tags
  local old was_level level_change="" lines_changed=0 suffix pushed
  [ -n "$NEW_pi$NEW_cloud$NEW_web" ] ||
    die "indiquer au moins une version : --pi, --cloud ou --web"
  for c in $COMPONENTS; do
    eval "new=\$NEW_$c"
    [ -z "$new" ] || matches "$new" "^$SEMVER\$" ||
      die "version invalide pour $c : '$new' (attendu X.Y.Z)"
  done
  if [ -n "$NEW_pi" ]; then
    [ -n "$PI_LEVEL" ] ||
      die "une version du Pi exige --pi-validation (bench, auto_validated ou occupied_validated)"
    is_level "$PI_LEVEL" || die "niveau de validation inconnu : '$PI_LEVEL'"
  elif [ -n "$PI_LEVEL" ]; then
    die "--pi-validation ne s'applique qu'avec --pi"
  fi
  [ -z "$RELEASE_DATE" ] || matches "$RELEASE_DATE" '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' ||
    die "date invalide : '$RELEASE_DATE' (attendu AAAA-MM-JJ)"
  today="$(date -u +%Y-%m-%d)"

  start prepare
  ref="$REMOTE/$DEVELOP"
  sha="$(sha_of "$ref")"
  git show "$ref:CHANGELOG.md" >"$TMP/changelog.md" 2>/dev/null ||
    die "CHANGELOG.md absent de $ref"

  : >"$TMP/sections.md"
  : >"$TMP/released"
  : >"$TMP/files"
  files=""
  changed=0
  for c in $COMPONENTS; do
    eval "new=\$NEW_$c"
    [ -n "$new" ] || continue
    cur="$(current_version "$c" "$ref")"
    matches "$cur" "^$SEMVER(-dev)?\$" ||
      die "version courante de $c illisible dans $ref : '$cur'"
    when="${RELEASE_DATE:-$today}"
    resumed=0
    if [ "$new" = "$cur" ] && ! tag_exists "$c-$new"; then
      # Reprise : la version est déjà écrite dans develop et attend son tag
      # (develop a bougé depuis le premier prepare). Seule sa section est
      # recalculée ; elle garde sa date, sauf --date.
      resumed=1
      was="$(sed -n "s/^## $c-$new (\\([0-9-]*\\))\$/\\1/p" "$TMP/changelog.md" | head -n 1)"
      when="${RELEASE_DATE:-${was:-$today}}"
      printf '%s-%s : déjà écrite dans %s, seule sa section de CHANGELOG.md est recalculée\n' "$c" "$new" "$ref" >>"$TMP/files"
    else
      version_gt "$new" "$cur" ||
        die "$c-$new n'est pas supérieure à la version courante $c-$cur"
      ! tag_exists "$c-$new" || die "le tag $c-$new existe déjà"
      if [ "${cur%-dev}" = "$cur" ]; then
        tag_exists "$c-$cur" ||
          die "$c-$cur n'a pas de tag : terminer la release précédente (scripts/release.sh tag)"
      fi
      changed=1
      case "$c" in
        pi)
          printf 'raspberry-pi/VERSION : pi-%s -> pi-%s\n' "$cur" "$new" >>"$TMP/files"
          files="$files raspberry-pi/VERSION"
          ;;
        cloud)
          printf 'convex/VERSION : cloud-%s -> cloud-%s\n' "$cur" "$new" >>"$TMP/files"
          printf 'convex/cloudVersion.ts : CLOUD_VERSION = "cloud-%s"\n' "$new" >>"$TMP/files"
          files="$files convex/VERSION convex/cloudVersion.ts"
          ;;
        web)
          printf 'package.json, package-lock.json : version %s -> %s\n' "$cur" "$new" >>"$TMP/files"
          files="$files package.json package-lock.json"
          ;;
      esac
    fi
    base="$(base_tag "$c" "$new")" || exit 1
    if [ -n "$base" ]; then
      since="\`$base\`"
      range="$base..$ref"
    else
      since="la première version"
      range="$ref"
    fi
    require_titled_reverts "$c" "$range"
    [ ! -s "$TMP/sections.md" ] || printf '\n' >>"$TMP/sections.md"
    section "$c" "$new" "$when" "$PI_LEVEL" "$since" "$range" >"$TMP/section-$c.md" ||
      die "lecture de l'historique impossible pour $range"
    if [ "$resumed" -eq 1 ]; then
      old="$(extract_section "$c-$new" <"$TMP/changelog.md")"
      if [ "$(cat "$TMP/section-$c.md")" != "$old" ]; then
        changed=1
        # Ce qui change dans la section : ses lignes de PR de ticket, ou autre chose.
        [ "$(grep '^- ' "$TMP/section-$c.md")" = "$(printf '%s\n' "$old" | grep '^- ')" ] ||
          lines_changed=1
      fi
      # La reprise accepte un autre niveau de validation que celui de la
      # section : c'est une décision, pas un complément de changelog, et le
      # titre comme le texte de la PR doivent le dire.
      if [ "$c" = pi ]; then
        was_level="$(printf '%s\n' "$old" | level_of_section)"
        [ "$was_level" = "$PI_LEVEL" ] ||
          level_change="\`${was_level:-aucun niveau lisible}\` devient \`$PI_LEVEL\`"
      fi
    fi
    cat "$TMP/section-$c.md" >>"$TMP/sections.md"
    printf '%s %s %s\n' "$c" "$new" "${PI_LEVEL:--}" >>"$TMP/released"
  done
  [ "$changed" -eq 1 ] ||
    die "rien à changer : les sections de $ref citent déjà chaque PR de ticket de ces versions"

  remove_sections "$(tags_of "$TMP/released" ' ')" <"$TMP/changelog.md" |
    insert_sections "$TMP/sections.md" >"$TMP/changelog.new" ||
    die "CHANGELOG.md de $ref n'a plus sa ligne repère : $MARKER"

  require_green "$sha" "$DEVELOP"

  tags="$(tags_of "$TMP/released" ', ')"
  if [ -n "$files" ]; then
    branch="release/$(tags_of "$TMP/released" _)"
    title="Release : $tags"
  else
    branch="release/$(tags_of "$TMP/released" _)-changelog-$(git rev-parse --short "$sha")"
    # Le titre dit ce que la PR change : des lignes de PR de ticket, le niveau
    # de validation du Pi, ou les deux.
    suffix="changelog complété"
    if [ -n "$level_change" ]; then
      suffix="niveau de validation du Pi modifié"
      [ "$lines_changed" -eq 0 ] || suffix="changelog complété, $suffix"
    fi
    title="Release : $tags ($suffix)"
  fi
  {
    if [ -n "$files" ]; then
      printf 'Prépare la release %s.\n\n' "$tags"
      printf 'Écrit par `scripts/release.sh prepare` depuis `%s` (%s), CI %s.\n' "$DEVELOP" "$sha" "$CI_SUMMARY"
      printf 'Cette PR ne change que les fichiers de version et `CHANGELOG.md`.\n'
    else
      if [ "$lines_changed" -eq 1 ]; then
        printf 'Complète le changelog de la release %s : `%s` a reçu des PR de ticket depuis sa préparation.\n\n' "$tags" "$DEVELOP"
      else
        printf "Réécrit le changelog de la release %s sans y ajouter de PR de ticket.\n\n" "$tags"
      fi
      if [ -n "$level_change" ]; then
        printf '**Change le niveau de validation de `pi-%s` : %s.** ' "$NEW_pi" "$level_change"
        printf "Ce n'est pas un complément de changelog : la ligne « Niveau de validation du Pi justifié » de la check-list de release est à prouver pour ce niveau.\n\n"
      fi
      printf 'Écrit par `scripts/release.sh prepare` depuis `%s` (%s), CI %s.\n' "$DEVELOP" "$sha" "$CI_SUMMARY"
      printf 'Cette PR ne change que `CHANGELOG.md`.\n'
    fi
    printf "Ne rien fusionner d'autre dans \`%s\` avant la fusion de la release dans \`%s\`.\n" "$DEVELOP" "$MAIN"
    printf 'Après sa fusion : `scripts/release.sh pr`, puis la check-list de `docs/release.md`.\n\n'
    cat "$TMP/sections.md"
  } >"$TMP/pr-prepare.md"
  release_pr_body "le commit de \`$DEVELOP\` qui suivra la fusion de la PR de préparation" \
    "son état sera lu à l'ouverture de la PR ($REQUIRED_STATUSES)"
  stage_prepared "$ref" "$files"

  # Un prepare précédent a pu pousser cette branche puis échouer à créer sa PR.
  # Ce que origin en sait est lu sur origin même : une référence locale peut
  # survivre à une branche supprimée.
  pushed="$(git ls-remote "$REMOTE" "refs/heads/$branch" | cut -f 1)" ||
    die "lecture des branches de $REMOTE impossible"
  if [ -n "$pushed" ]; then
    git fetch --quiet "$REMOTE" "refs/heads/$branch" ||
      die "lecture de la branche $branch de $REMOTE impossible"
    same_preparation "$pushed" "$sha" "$title" "$files" ||
      die "la branche $branch existe déjà sur $REMOTE avec un autre contenu que celui de cette préparation : fermer sa PR s'il y en a une, la supprimer (git push $REMOTE --delete $branch), puis relancer"
  fi

  printf 'Candidat : %s @ %s\nCI : %s\n' "$ref" "$sha" "$CI_SUMMARY"
  heading "Fichiers de version"
  cat "$TMP/files"
  heading "CHANGELOG.md : sections écrites sous la ligne repère"
  cat "$TMP/sections.md"
  heading "Commit et PR de préparation"
  if [ -n "$pushed" ]; then
    printf 'Reprise : la branche %s est déjà sur %s avec ce contenu (%s). Seule sa PR reste à créer.\n' "$branch" "$REMOTE" "$pushed"
  else
    printf 'git switch -c %s %s\n' "$branch" "$ref"
    printf 'git commit -m "%s"  (fichiers : CHANGELOG.md%s)\n' "$title" "$files"
    printf 'git push -u %s %s\n' "$REMOTE" "$branch"
  fi
  printf 'gh pr create --base %s --head %s --title "%s"\n' "$DEVELOP" "$branch" "$title"
  printf 'Corps de la PR :\n'
  sed 's/^/  | /' "$TMP/pr-prepare.md"
  heading "Ensuite : scripts/release.sh pr (après fusion dans $DEVELOP)"
  printf 'gh pr create --base %s --head %s --title "Release : %s"\n' "$MAIN" "$DEVELOP" "$tags"
  printf 'Corps de la PR :\n'
  sed 's/^/  | /' "$TMP/pr-release.md"
  heading "Ensuite : scripts/release.sh tag (après fusion dans $MAIN)"
  while read -r c new _; do
    printf 'git tag -a %s-%s <commit de %s>\n' "$c" "$new" "$MAIN"
    tag_message "$c" "$new" | sed 's/^/  | /'
  done <"$TMP/released"
  printf 'git push --atomic %s %s\n' "$REMOTE" "$(tags_of "$TMP/released" ' ')"

  [ "$DRY_RUN" -eq 0 ] || return 0

  if [ -n "$pushed" ]; then
    heading "Fait"
    open_prepare_pr "$branch" "$title" ||
      die "la PR de préparation n'a pas pu être créée : la branche $branch reste sur $REMOTE, relancer la même commande"
    return 0
  fi

  git diff --quiet && git diff --cached --quiet ||
    die "l'arbre de travail a des modifications : les valider ou les retirer d'abord"
  ! git rev-parse -q --verify "refs/heads/$branch" >/dev/null ||
    die "la branche $branch existe déjà dans ce clone sans être sur $REMOTE : la supprimer (git branch -D $branch), puis relancer"
  # Où revenir si la suite échoue : la branche courante, ou son commit.
  PREP_BACK="$(git symbolic-ref -q --short HEAD)" ||
    PREP_BACK="--detach $(git rev-parse HEAD)"
  git switch --quiet --no-track -c "$branch" "$ref"
  PREP_BRANCH="$branch"
  # shellcheck disable=SC2086
  for c in CHANGELOG.md $files; do
    cp "$TMP/out/$c" "$c" || die "écriture de $c impossible"
  done
  # shellcheck disable=SC2086
  git add -- CHANGELOG.md $files
  git commit --quiet -m "$title" || die "le commit de préparation a été refusé"
  git push --quiet -u "$REMOTE" "$branch" ||
    die "la branche $branch n'a pas pu être poussée sur $REMOTE"
  PREP_PUSHED=1
  heading "Fait"
  open_prepare_pr "$branch" "$title" ||
    die "la PR de préparation n'a pas pu être créée"
  # Réussi : le clone reste sur la branche de préparation, comme avant.
  PREP_BRANCH=""
}

cmd_pr() {
  local ref sha title review
  start pr
  ref="$REMOTE/$DEVELOP"
  sha="$(sha_of "$ref")"
  collect_pending "$ref"
  [ "$(git rev-list --count "$REMOTE/$MAIN..$ref")" -gt 0 ] ||
    die "$ref n'a aucun commit de plus que $REMOTE/$MAIN"
  require_complete_sections "$sha" "puis relancer scripts/release.sh pr."
  require_green "$sha" "$DEVELOP"
  # L'avis indépendant n'est pas exigé ici : il se rend sur le candidat que
  # cette PR montre. Son état à l'ouverture est écrit dans la PR ; main l'exige
  # réussi pour fusionner, et tag le vérifie avant de poser un tag.
  review="$(printf '%s\n' "$COMMIT_STATUSES" | review_summary)"
  title="Release : $(tags_of "$TMP/released" ', ')"
  release_pr_body "$sha, CI $CI_SUMMARY" "à l'ouverture de cette PR, $review"

  printf 'Candidat : %s @ %s\nCI : %s\nAvis indépendant : %s\n' "$ref" "$sha" "$CI_SUMMARY" "$review"
  heading "PR de release"
  printf 'gh pr create --base %s --head %s --title "%s"\n' "$MAIN" "$DEVELOP" "$title"
  printf 'Corps de la PR :\n'
  sed 's/^/  | /' "$TMP/pr-release.md"

  [ "$DRY_RUN" -eq 0 ] || return 0
  heading "Fait"
  gh pr create --base "$MAIN" --head "$DEVELOP" --title "$title" --body-file "$TMP/pr-release.md"
}

drop_local_tags() {
  local c v
  while read -r c v _; do
    git tag -d "$c-$v" >/dev/null 2>&1 || true
  done <"$TMP/released"
}

cmd_tag() {
  local ref sha c v level refs now prepared tip
  start tag
  ref="$REMOTE/$MAIN"
  sha="$(sha_of "$ref")"
  # La pipeline de release a testé un commit précis : si main a bougé depuis,
  # les tags iraient sur un commit qu'elle n'a pas testé.
  [ -z "$EXPECT_COMMIT" ] || [ "$sha" = "$EXPECT_COMMIT" ] ||
    die "$ref est au commit $sha, pas au commit attendu ($EXPECT_COMMIT) : $MAIN a bougé depuis le lancement. Aucun tag n'est posé."
  collect_pending "$ref"
  # Une fusion en squash couperait main de l'historique de develop : le
  # changelog suivant, calculé depuis ces tags, reprendrait tout depuis le début.
  prepared="$(git log -1 --first-parent --format=%H "$REMOTE/$DEVELOP" -- CHANGELOG.md)"
  { [ -n "$prepared" ] && git merge-base --is-ancestor "$prepared" "$ref"; } ||
    die "$ref ne contient pas le commit de $REMOTE/$DEVELOP qui a écrit CHANGELOG.md en dernier (${prepared:-introuvable}). Soit la PR de release a été fusionnée en squash (elle doit l'être par commit de fusion), soit $DEVELOP porte une préparation plus récente : ouvrir et fusionner une PR vers $MAIN (scripts/release.sh pr)"
  tip="$(develop_tip_in "$sha")" ||
    die "$ref ne contient aucun commit de $REMOTE/$DEVELOP"
  require_complete_sections "$tip" "ouvrir et fusionner une nouvelle PR vers $MAIN (scripts/release.sh pr), puis relancer scripts/release.sh tag."
  require_green "$sha" "$MAIN"
  # Le commit de fusion de main est neuf : personne ne l'a relu. L'avis porte
  # sur le commit de develop qu'il a fait entrer.
  require_review "$tip"

  printf 'Cible : %s @ %s\nCI : %s\nAvis indépendant : %s réussi sur le candidat %s\n' "$ref" "$sha" "$CI_SUMMARY" "$REQUIRED_STATUSES" "$tip"
  heading "Tags annotés"
  refs=""
  while read -r c v _; do
    tag_message "$c" "$v" >"$TMP/tag-$c.txt"
    printf 'git tag -a %s-%s %s\n' "$c" "$v" "$sha"
    sed 's/^/  | /' "$TMP/tag-$c.txt"
    refs="$refs refs/tags/$c-$v"
  done <"$TMP/released"
  printf 'git push --atomic %s%s\n' "$REMOTE" "$refs"

  now="$(date +%s)000"
  heading "Reste à faire à la main (docs/release.md, étapes 8 et 9)"
  printf 'Déployer Convex puis le site dans la fenêtre fixée. Ensuite, enregistrer chaque version dans Convex, mutation admin softwareReleases:recordRelease :\n'
  while read -r c v level; do
    if [ "$c" = pi ]; then
      printf '  {"component":"pi","version":"pi-%s","validationLevel":"%s","releasedAt":%s}\n' "$v" "$level" "$now"
    else
      printf '  {"component":"%s","version":"%s-%s","releasedAt":%s}\n' "$c" "$c" "$v" "$now"
    fi
  done <"$TMP/released"

  [ "$DRY_RUN" -eq 0 ] || return 0
  # Tout ou rien, en local comme sur origin. Un tag resté en local ferait croire
  # à la release suivante que cette version est publiée ; un tag accepté par
  # origin quand un autre y est refusé laisserait une release à moitié posée.
  # --atomic : origin prend tous les tags, ou n'en prend aucun.
  while read -r c v _; do
    git tag -a --cleanup=whitespace -F "$TMP/tag-$c.txt" "$c-$v" "$sha" || {
      drop_local_tags
      die "le tag $c-$v n'a pas pu être créé : aucun tag n'est conservé"
    }
  done <"$TMP/released"
  # shellcheck disable=SC2086
  git push --quiet --atomic "$REMOTE" $refs || {
    drop_local_tags
    die "les tags n'ont pas pu être poussés : $REMOTE n'en a reçu aucun, et aucun n'est conservé en local"
  }
  heading "Fait"
  printf 'Tags poussés :%s\n' "$refs"
}

[ $# -gt 0 ] || { usage; exit 2; }
STEP="$1"
shift
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --pi | --cloud | --web | --pi-validation | --date)
      [ $# -ge 2 ] || die "valeur manquante après $1"
      [ "$STEP" = prepare ] || die "$1 ne s'applique qu'à prepare"
      case "$1" in
        --pi) NEW_pi="$2" ;;
        --cloud) NEW_cloud="$2" ;;
        --web) NEW_web="$2" ;;
        --pi-validation) PI_LEVEL="$2" ;;
        --date) RELEASE_DATE="$2" ;;
      esac
      shift
      ;;
    --expect-commit | --pipeline-run)
      [ $# -ge 2 ] || die "valeur manquante après $1"
      [ "$STEP" = tag ] || die "$1 ne s'applique qu'à tag"
      case "$1" in
        --expect-commit)
          matches "$2" '^[0-9a-f]{40}$' ||
            die "commit invalide après $1 : '$2' (attendu : un SHA complet)"
          EXPECT_COMMIT="$2"
          ;;
        --pipeline-run)
          matches "$2" '^[1-9][0-9]*$' ||
            die "numéro d'exécution invalide après $1 : '$2'"
          PIPELINE_RUN="$2"
          ;;
      esac
      shift
      ;;
    -h | --help) usage; exit 0 ;;
    *) die "option inconnue : $1 (voir --help)" ;;
  esac
  shift
done

case "$STEP" in
  prepare) cmd_prepare ;;
  pr) cmd_pr ;;
  tag) cmd_tag ;;
  -h | --help) usage ;;
  *) die "étape inconnue : $STEP (prepare, pr ou tag)" ;;
esac
