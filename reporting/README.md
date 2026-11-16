# Reporting

Application durable de pilotage des projets et de la rentabilite.

- Interface : Synthese, Collaborateurs, Projets et Administration.
- Acquisitions Odoo/Sellsy en lecture seule via `core/`.
- Recalcul strictement local, sans synchronisation vers Odoo.
- Port local : `4174`.

Lancement seul : `reporting\start.cmd`.

Pour demarrer l'application et ouvrir le navigateur, double-cliquer sur
`lancer_application.vbs` a la racine du projet.

Les revisions de sources sont conservees dans `data/`, hors Git. Le runtime
local est `reporting/runtime/`.
