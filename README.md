# Mandarine Temps Collab

Application locale de reporting des temps, couts, facturations et projets.

## Demarrage

- `lancer_application.vbs` : demarre Reporting et ouvre le navigateur ;
- `start_local.cmd` : demarre le serveur sans recalcul ;
- `refresh_local.cmd` : recalcule les rapports depuis les sources locales ;
- `deploy_local.cmd` : execute les tests, recalcule et redemarre.

L'interface est disponible par defaut sur <http://127.0.0.1:4174/>.

## Architecture

- `reporting/` : serveur, interface et calcul des rapports ;
- `core/` : acquisitions Odoo/Sellsy en lecture seule et gestion des revisions ;
- `data/` : sources locales et pointeurs actifs, volontairement exclus de Git ;
- `tools/` : commandes de demarrage, recalcul et deploiement local.

La synchronisation Sellsy vers Odoo est une application autonome et ne fait pas
partie de ce depot.

## Configuration

Copier `.env.example` vers `.env`, renseigner les identifiants, puis installer
les dependances de `requirements.txt` dans l'environnement Python local.

Les secrets, donnees sources, journaux et rapports calcules ne sont pas suivis
par Git.
