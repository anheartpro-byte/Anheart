---
name: anheart-ticket
description: Coordonner un ticket Anheart avec des Tasks Orca et une checklist sur le parent Linear existant, workers Opus 5.5, tests, revue et integration. Aucun nouveau ticket Linear. Charger les instructions partagees avant toute action.
---
> **Mise a jour du 29 octobre 2026.** Depuis le 23 octobre, tous les roles (pilote, coordinateurs, developpeurs, executeurs de tests, relecteurs R1 et R2) tournent en Claude Opus 5.5 (`claude-opus-5-5[1m]`), R1 et R2 dans des sessions distinctes : Codex connecte par un compte ChatGPT refuse `gpt-6.1-sol`. Les mentions de GPT-6.1-Sol et de Luna plus bas sont historiques. Voir `docs/handoff.md`.


# Anheart - Coordinateur ticket

Lis et applique le skill canonique
[anheart-ticket](../../../.agents/skills/anheart-ticket/SKILL.md), avec ses
references et la configuration partagee. Resous ce lien depuis ce fichier.

Ce point d'entree rend les memes regles accessibles a Claude ; il ne definit
pas un second workflow. Si les fichiers ne sont pas presents dans le worktree,
utilise la racine absolue transmise par le pilote ; sinon signale leur absence.
Respecte le Dispatch Orca et le perimetre de la demande ; ne lance rien lors
d'une simple preparation ou installation.
