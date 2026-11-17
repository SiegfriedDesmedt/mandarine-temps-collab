# Cartographie de l’architecture

## Vue d’ensemble

Temps Collab est une application locale composée de quatre blocs actifs :

| Bloc | Technologies | Responsabilité |
|---|---|---|
| Interface | HTML, CSS, JavaScript sans framework | Affichage, filtres, exports CSV et actions d’administration |
| Backend Reporting | Python, `ThreadingHTTPServer` | Fichiers statiques, API locale et orchestration des tâches |
| Noyau `core` | Python, SQLite, XML-RPC, HTTP Sellsy, `openpyxl` | Acquisitions incrémentales en lecture seule et gestion des révisions |
| Stockage local | JSON, SQLite, CSV/XLSX historiques | Sources immuables, pointeurs actifs, historique et rapport publié |

```text
Odoo XML-RPC ──┐
               ├─> acquisition ─> révision validée ─┐
Sellsy API ────┘                                     │
                                                     v
                                      data/active_sources.json
                                                     │
                                                     v
                                  calcul + validation du rapport
                                                     │
                                                     v
                                      reporting/dist/data.json
                                                     │
                                                     v
                                             navigateur local
```

L’acquisition ne déclenche pas le recalcul. Le rapport continue donc d’utiliser
sa dernière publication jusqu’à une actualisation explicite.

## Cartographie des composants

### `core/`

| Module | Responsabilité |
|---|---|
| `env_loader.py` | Lecture des fichiers dotenv sans exposer les secrets |
| `odoo_exporter.py` | Extraction complète ou incrémentale de huit jeux Odoo |
| `sellsy_client.py` | Client Sellsy à endpoints et méthodes autorisés |
| `sellsy_importer.py` | Extraction complète ou incrémentale du Sellsy principal et des commandes Mandarine Expérience |
| `source_database.py` | Schémas SQLite, migration historique, UPSERT et validation d'intégrité |
| `synchronization_check.py` | Contrôles distants bornés et strictement en lecture seule |
| `sellsy_scope.py` | Borne temporelle Sellsy au 1er janvier 2021 |
| `source_revision_store.py` | Staging, manifestes, empreintes et promotion atomique |
| `snapshot_store.py` | Résolution des onze sources logiques, SQLite ou historiques |
| `integration_store.py` | Historique JSON des acquisitions et recalculs |
| `paths.py` | Chemins racine du dépôt et du stockage `data/` |

### `reporting/backend/`

| Module | Responsabilité |
|---|---|
| `server.py` | Serveur HTTP, sécurité locale et orchestration asynchrone |
| `report_builder.py` | Rapprochements, agrégats, contrôles et publication JSON |
| `source_freshness.py` | Âge, taille, nombre de lignes et état des sources |

### `tools/`

| Module | Responsabilité |
|---|---|
| `run_local.ps1` | Sélectionne un Python disposant de `openpyxl` |
| `local_environment.py` | Valide, démarre, recalcule, teste et redémarre |
| `lancer_application.ps1` | Lance Reporting et ouvre le navigateur |

## Stockage et publications

```text
data/
├── active_sources.json
└── source-revisions/
    ├── odoo/<revision-id>/
    │   ├── manifest.json
    │   └── odoo.sqlite.gz
    └── sellsy/<revision-id>/
        ├── manifest.json
        └── sellsy.sqlite.gz

reporting/
├── dist/data.json
└── runtime/
    ├── integration_history.json
    ├── local-server.json
    ├── local-server.out.log
    ├── local-server.err.log
    ├── odoo_export.log
    ├── sellsy_import.log
    └── report-rebuild.lock       # présent uniquement pendant un recalcul
```

`data/`, `reporting/runtime/` et `reporting/dist/data.json` sont exclus de Git.
Un clone du dépôt contient donc le code, mais pas les données nécessaires au
fonctionnement métier.

## API HTTP

| Méthode et route | Rôle | Protection |
|---|---|---|
| `GET /api/health` | Santé du serveur et jeton d’instance interne | Publique |
| `GET /api/admin/status` | Configuration, fraîcheur, révisions et historique | Administration |
| `GET /api/admin/integrations?limit=N` | Historique technique | Administration |
| `GET /api/admin/integrations/<id>` | État d’une tâche asynchrone | Administration |
| `POST /api/admin/integrations/odoo-timesheets` | Lance l’acquisition Odoo | Administration |
| `POST /api/admin/integrations/sellsy-sources` | Lance l’acquisition Sellsy | Administration |
| `POST /api/admin/integrations/sync-check` | Contrôle borné Odoo/Sellsy sans écriture | Administration |
| `POST /api/admin/report/rebuild` | Lance le recalcul local | Administration |

Sans `ADMIN_API_TOKEN`, les routes d’administration n’acceptent que les clients
loopback (`127.0.0.1` ou `::1`). Si un jeton est configuré, il doit être envoyé
par `Authorization: Bearer …` ou `X-Admin-Token`. L’interface web actuelle
n’injecte pas de jeton : son administration est conçue pour l’usage local sans
jeton.

Le serveur refuse une écoute non locale sans `ADMIN_API_TOKEN`. Une écoute
réseau expose néanmoins les fichiers statiques, dont le rapport calculé ; elle
ne doit donc pas être activée sans contrôle réseau adapté.

## Concurrence et reprise

- une seule acquisition, reconstruction ou vérification distante peut être active à la fois ;
- une demande concurrente reçoit HTTP `409 Conflict` avec l’identifiant actif ;
- une révision incomplète reste en staging et n’est jamais activée ;
- une erreur conserve la révision et le rapport précédents ;
- au démarrage, les tâches restées `queued` ou `running` sont clôturées comme
  interrompues ;
- le rapport est publié par remplacement atomique après validation complète.

Une acquisition incrémentale décompresse la base active dans le staging, applique les
UPSERT, vérifie `PRAGMA integrity_check`, puis promeut la nouvelle révision. Une
acquisition complète repart d'une base vide et sert de réconciliation des
suppressions. Les CSV/XLSX ne sont plus le format nominal mais restent pris en
charge pendant la transition. La base validée est compressée avec `gzip` avant
promotion et matérialisée temporairement une seule fois pendant un recalcul.

## Frontières du produit

Le dépôt ne contient plus :

- la synchronisation Sellsy vers Odoo ;
- la préparation ou la création de devis Odoo ;
- une route d’écriture ERP ;
- une vue métier « Qualité » séparée.

Les contrôles de cohérence nécessaires aux trois rapports restent présents sous
forme de compteurs et d’alertes.
