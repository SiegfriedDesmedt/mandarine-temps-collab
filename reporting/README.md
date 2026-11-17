# Composant Reporting

`reporting/` contient l’application active : le serveur HTTP local, la SPA
statique, le calcul du rapport et les tests de non-régression.

## Structure

| Chemin | Rôle |
|---|---|
| `backend/server.py` | Sert l’interface et expose les routes d’administration |
| `backend/report_builder.py` | Lit les onze sources et construit le contrat JSON |
| `backend/source_freshness.py` | Calcule la fraîcheur et l’état des fichiers |
| `dist/index.html` | Structure de la SPA |
| `dist/app.js` | Vues, filtres, exports CSV et administration |
| `dist/styles.css` | Présentation responsive |
| `dist/data.json` | Publication calculée, générée localement et exclue de Git |
| `runtime/` | État du serveur, historique et journaux, exclus de Git |
| `tests/` | Tests unitaires, frontières applicatives et déploiement local |

## Vues exposées

1. **Synthèse des Facturations et Coûts** : indicateurs financiers, évolution
   mensuelle, répartition par BU et points de vigilance.
2. **Activité des collaborateurs** : heures, coût direct, ventilation
   client/interne/absence et contrôle mensuel de complétude.
3. **Projets & clients** : liste filtrable et fiche détaillée comprenant tâches,
   commandes, factures, avoirs, temps, coûts fixes et marges.
4. **Administration** : état des intégrations, acquisitions incrémentales,
   réconciliation complète Odoo ou Sellsy à la demande, contrôle distant en lecture seule,
   fraîcheur des sources, historique et recalcul.

Les filtres globaux couvrent l’exercice fiscal, la BU et la recherche. Les vues
Collaborateurs et Projets ajoutent leurs propres tris, filtres et exports CSV.

## Backend local

Le serveur écoute par défaut sur `127.0.0.1:4174`. Il sert les fichiers de
`dist/` et les routes `/api/*`. Une seule acquisition ou reconstruction peut
être active à la fois ; les tâches longues sont exécutées dans un thread et
leur progression est conservée dans `runtime/integration_history.json`.

Le recalcul :

1. résout les deux révisions actives, SQLite ou CSV/XLSX historiques ;
2. empreinte leurs fichiers physiques en SHA-256 ;
3. calcule et valide le contrat JSON ;
4. vérifie que les sources n’ont pas changé ;
5. remplace atomiquement `dist/data.json`.

Un verrou inter-processus empêche deux reconstructions simultanées et est
récupéré si son processus propriétaire n’existe plus.

Les nouvelles acquisitions produisent `odoo.sqlite.gz` et `sellsy.sqlite.gz`. Les
lecteurs transforment leurs tables en dictionnaires compatibles avec le calcul
historique, de sorte que le contrat `data.json` et les règles métier ne changent
pas. Les anciennes révisions CSV/XLSX restent directement lisibles.

## Lancement

Depuis la racine, utiliser de préférence `start_local.cmd`,
`refresh_local.cmd` ou `deploy_local.cmd`. `reporting\start.cmd` lance le serveur
au premier plan avec le Python disponible dans `PATH`, sans les contrôles du
lanceur principal.

Voir aussi [l’architecture générale](../documentation/architecture.md) et le
[guide d’exploitation](../documentation/exploitation-locale.md).
