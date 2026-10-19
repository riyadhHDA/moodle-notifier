# moodle-notifier (GitHub Actions)

Surveille 10 cours Moodle Unistra toutes les 5 minutes via l'API Web Services
et envoie un e-mail groupé (Brevo SMTP) quand du nouveau contenu apparaît.

Fonctionne **sans serveur** : un workflow GitHub Actions s'exécute selon un
cron, compare le contenu des cours à `state.json` (commité dans ce dépôt) et
envoie la notification. Aucun quota payant : GitHub Actions est gratuit pour
les dépôts publics.

## Fichiers

| Fichier | Rôle |
|---|---|
| `moodle_notifier.py` | le bot (aucune modification par rapport au VPS) |
| `config/courses.yml` | les 10 cours surveillés (IDs stables) |
| `state.json` | état anti-doublon, versionné, mis à jour par le workflow |
| `.github/workflows/moodle-notifier.yml` | planification + exécution |
| `moodle-notifier.env` | secrets (créé à l'exécution depuis le secret `MOODLE_ENV`, jamais commité) |

## Secret requis

`MOODLE_ENV` : contenu complet du fichier `moodle-notifier.env`
(`MOODLE_URL`, `MOODLE_TOKEN`, `SMTP_*`, `MAIL_FROM`, `MAIL_TO`, `CHECK_INTERVAL`).

## Remarques

- Le premier passage initialise `state.json` en silence : aucun e-mail pour
  le contenu historique.
- Si l'envoi SMTP échoue, l'état n'est pas enregistré : nouvelle tentative au
  passage suivant.
- Un commit `.keepalive` est créé le 1er de chaque mois pour éviter la
  désactivation des workflows planifiés après 60 jours sans activité.
