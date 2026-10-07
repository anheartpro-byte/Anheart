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
# Compatible bash 3.2 (macOS) : ni tableau associatif, ni mapfile.
set -euo pipefail

REMOTE=origin
DEVELOP="${RELEASE_DEVELOP:-develop}"
MAIN="${RELEASE_MAIN:-main}"
COMPONENTS="pi cloud web"
# Les jobs de .github/workflows/ci.yml sans lesquels un commit n'est pas vert.
# Si un job est renommé, cette liste doit suivre : sinon le script refuse tout.
REQUIRED_CHECKS="pi-gate simulation-gate convex-tests web audit docs"
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

usage() {
  cat <<'EOF'
Usage :
  scripts/release.sh prepare [--pi X.Y.Z] [--cloud X.Y.Z] [--web X.Y.Z]
                             [--pi-validation bench|auto_validated|occupied_validated]
                             [--date AAAA-MM-JJ] [--dry-run]
  scripts/release.sh pr  [--dry-run]
  scripts/release.sh tag [--dry-run]

prepare  depuis origin/develop vert : fichiers de version, CHANGELOG.md, PR vers develop.
         Relancé avec les mêmes versions quand develop a bougé, il complète les sections.
pr       après fusion de la préparation : PR develop -> main (modèle de release).
tag      après fusion dans main : tags annotés pi-X.Y.Z, cloud-X.Y.Z, web-X.Y.Z.

pr et tag refusent si le commit à publier contient une PR de ticket d'un
composant que la section de sa version ne cite pas.

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
# Seuls les check runs du commit sont lus. Les statuts de commit (les
# déploiements Vercel, par exemple) ne le sont pas : le responsable de release
# les regarde dans la PR.
require_green() { # sha branche
  local runs bad missing name total
  runs="$(gh api "repos/{owner}/{repo}/commits/$1/check-runs" --paginate \
    --jq '.check_runs[] | [.name, .status, (.conclusion // "")] | @tsv')" ||
    die "lecture des vérifications CI impossible pour $2 ($1)"
  [ -n "$runs" ] || die "aucune vérification CI pour $2 ($1) : attendre la CI"
  bad="$(printf '%s\n' "$runs" | awk -F '\t' '
    $2 != "completed" || ($3 != "success" && $3 != "skipped" && $3 != "neutral") {
      printf "  - %s : %s %s\n", $1, $2, $3
    }')"
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
  CI_SUMMARY="verte ($total vérifications terminées, toutes les gates réussies)"
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

versions_table() { # candidat ; lignes "composant version niveau" sur l'entrée standard
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
  trap 'rm -rf "$TMP"' EXIT
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

release_pr_body() { # candidat
  versions_table "$1" <"$TMP/released" >"$TMP/versions.md"
  git show "$REMOTE/$DEVELOP:$TEMPLATE" >"$TMP/template.md" 2>/dev/null ||
    die "$TEMPLATE absent de $REMOTE/$DEVELOP"
  fill_template "$TMP/versions.md" "$TMP/sections.md" <"$TMP/template.md" >"$TMP/pr-release.md" ||
    die "$TEMPLATE n'a plus ses deux emplacements release.sh"
}

tag_message() { # composant version
  printf '%s-%s\n\n' "$1" "$2"
  sed '1,2d' "$TMP/section-$1.md"
}

cmd_prepare() {
  local c new cur ref sha base since range branch title files
  local today when was resumed changed tags
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
    [ ! -s "$TMP/sections.md" ] || printf '\n' >>"$TMP/sections.md"
    section "$c" "$new" "$when" "$PI_LEVEL" "$since" "$range" >"$TMP/section-$c.md" ||
      die "lecture de l'historique impossible pour $range"
    if [ "$resumed" -eq 1 ] &&
      [ "$(cat "$TMP/section-$c.md")" != "$(extract_section "$c-$new" <"$TMP/changelog.md")" ]; then
      changed=1
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
    title="Release : $tags (changelog complété)"
  fi
  {
    if [ -n "$files" ]; then
      printf 'Prépare la release %s.\n\n' "$tags"
      printf 'Écrit par `scripts/release.sh prepare` depuis `%s` (%s), CI %s.\n' "$DEVELOP" "$sha" "$CI_SUMMARY"
      printf 'Cette PR ne change que les fichiers de version et `CHANGELOG.md`.\n'
    else
      printf 'Complète le changelog de la release %s : `%s` a reçu des PR de ticket depuis sa préparation.\n\n' "$tags" "$DEVELOP"
      printf 'Écrit par `scripts/release.sh prepare` depuis `%s` (%s), CI %s.\n' "$DEVELOP" "$sha" "$CI_SUMMARY"
      printf 'Cette PR ne change que `CHANGELOG.md`.\n'
    fi
    printf "Ne rien fusionner d'autre dans \`%s\` avant la fusion de la release dans \`%s\`.\n" "$DEVELOP" "$MAIN"
    printf 'Après sa fusion : `scripts/release.sh pr`, puis la check-list de `docs/release.md`.\n\n'
    cat "$TMP/sections.md"
  } >"$TMP/pr-prepare.md"
  release_pr_body "le commit de \`$DEVELOP\` qui suivra la fusion de la PR de préparation"

  printf 'Candidat : %s @ %s\nCI : %s\n' "$ref" "$sha" "$CI_SUMMARY"
  heading "Fichiers de version"
  cat "$TMP/files"
  heading "CHANGELOG.md : sections écrites sous la ligne repère"
  cat "$TMP/sections.md"
  heading "Commit et PR de préparation"
  printf 'git switch -c %s %s\n' "$branch" "$ref"
  printf 'git commit -m "%s"  (fichiers : CHANGELOG.md%s)\n' "$title" "$files"
  printf 'git push -u %s %s\n' "$REMOTE" "$branch"
  printf 'gh pr create --base %s --head %s --title "%s"\n' "$DEVELOP" "$branch" "$title"
  printf 'Corps de la PR :\n'
  sed 's/^/  | /' "$TMP/pr-prepare.md"
  heading "Ensuite : scripts/release.sh pr (après fusion dans $DEVELOP)"
  printf 'gh pr create --base %s --head %s --title "%s"\n' "$MAIN" "$DEVELOP" "$title"
  printf 'Corps de la PR :\n'
  sed 's/^/  | /' "$TMP/pr-release.md"
  heading "Ensuite : scripts/release.sh tag (après fusion dans $MAIN)"
  while read -r c new _; do
    printf 'git tag -a %s-%s <commit de %s>\n' "$c" "$new" "$MAIN"
    tag_message "$c" "$new" | sed 's/^/  | /'
  done <"$TMP/released"
  printf 'git push %s %s\n' "$REMOTE" "$(tags_of "$TMP/released" ' ')"

  [ "$DRY_RUN" -eq 0 ] || return 0

  git diff --quiet && git diff --cached --quiet ||
    die "l'arbre de travail a des modifications : les valider ou les retirer d'abord"
  ! git rev-parse -q --verify "refs/heads/$branch" >/dev/null ||
    die "la branche $branch existe déjà"
  git switch --quiet --no-track -c "$branch" "$ref"
  case " $files " in *" raspberry-pi/VERSION "*)
    printf 'pi-%s\n' "$NEW_pi" >raspberry-pi/VERSION
    ;;
  esac
  case " $files " in *" convex/VERSION "*)
    printf 'cloud-%s\n' "$NEW_cloud" >convex/VERSION
    cloud_constant "$NEW_cloud" >convex/cloudVersion.ts
    ;;
  esac
  case " $files " in *" package.json "*)
    set_package_version "$NEW_web"
    ;;
  esac
  cp "$TMP/changelog.new" CHANGELOG.md
  # shellcheck disable=SC2086
  git add -- CHANGELOG.md $files
  git commit --quiet -m "$title"
  git push --quiet -u "$REMOTE" "$branch"
  heading "Fait"
  gh pr create --base "$DEVELOP" --head "$branch" --title "$title" --body-file "$TMP/pr-prepare.md"
}

cmd_pr() {
  local ref sha title
  start pr
  ref="$REMOTE/$DEVELOP"
  sha="$(sha_of "$ref")"
  collect_pending "$ref"
  [ "$(git rev-list --count "$REMOTE/$MAIN..$ref")" -gt 0 ] ||
    die "$ref n'a aucun commit de plus que $REMOTE/$MAIN"
  require_complete_sections "$sha" "puis relancer scripts/release.sh pr."
  require_green "$sha" "$DEVELOP"
  title="Release : $(tags_of "$TMP/released" ', ')"
  release_pr_body "$sha, CI $CI_SUMMARY"

  printf 'Candidat : %s @ %s\nCI : %s\n' "$ref" "$sha" "$CI_SUMMARY"
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

  printf 'Cible : %s @ %s\nCI : %s\n' "$ref" "$sha" "$CI_SUMMARY"
  heading "Tags annotés"
  refs=""
  while read -r c v _; do
    tag_message "$c" "$v" >"$TMP/tag-$c.txt"
    printf 'git tag -a %s-%s %s\n' "$c" "$v" "$sha"
    sed 's/^/  | /' "$TMP/tag-$c.txt"
    refs="$refs refs/tags/$c-$v"
  done <"$TMP/released"
  printf 'git push %s%s\n' "$REMOTE" "$refs"

  now="$(date +%s)000"
  heading "Reste à faire à la main (docs/release.md, section 6)"
  printf 'Enregistrer chaque version dans Convex, mutation admin softwareReleases:recordRelease :\n'
  while read -r c v level; do
    if [ "$c" = pi ]; then
      printf '  {"component":"pi","version":"pi-%s","validationLevel":"%s","releasedAt":%s}\n' "$v" "$level" "$now"
    else
      printf '  {"component":"%s","version":"%s-%s","releasedAt":%s}\n' "$c" "$c" "$v" "$now"
    fi
  done <"$TMP/released"

  [ "$DRY_RUN" -eq 0 ] || return 0
  # Tout ou rien : un tag resté en local ferait croire à la release suivante
  # que cette version est publiée.
  while read -r c v _; do
    git tag -a --cleanup=whitespace -F "$TMP/tag-$c.txt" "$c-$v" "$sha" || {
      drop_local_tags
      die "le tag $c-$v n'a pas pu être créé : aucun tag n'est conservé"
    }
  done <"$TMP/released"
  # shellcheck disable=SC2086
  git push --quiet "$REMOTE" $refs || {
    drop_local_tags
    die "les tags n'ont pas pu être poussés : aucun n'est conservé en local"
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
