# Mandarine Temps Collab

Application locale de pilotage des temps, des coûts, de la facturation et de
la rentabilité des projets. Elle consolide onze sources en lecture seule issues
d’Odoo et de Sellsy, puis publie un rapport statique consultable dans le
navigateur.

La synchronisation Sellsy vers Odoo est une application autonome. Ce dépôt ne
crée ni devis, ni commande, ni écriture dans Odoo ou Sellsy.

## Démarrage rapide

Prérequis : Windows, Python 3 et le paquet `openpyxl`.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Renseigner les identifiants dans `.env`, puis utiliser l’une des commandes.
Les variables optionnelles `SELLSY_EXPERIENCE_CLIENT_ID` et
`SELLSY_EXPERIENCE_CLIENT_SECRET` ajoutent les commandes de la société
Mandarine Expérience au Sellsy principal :

| Commande | Effet |
|---|---|
| `lancer_application.vbs` | Démarre Reporting en arrière-plan et ouvre le navigateur |
| `start_local.cmd` | Démarre ou réutilise le serveur sans recalculer les données |
| `refresh_local.cmd` | Recalcule `reporting/dist/data.json` depuis les révisions locales actives |
| `deploy_local.cmd` | Vérifie les sources, exécute les tests, recalcule puis redémarre |

L’application est disponible par défaut sur <http://127.0.0.1:4174/>.

## Périmètre fonctionnel

- **Synthèse** : commandes, chiffre d’affaires net, coûts, marge et alertes ;
- **Collaborateurs** : temps, coûts, répartition mensuelle et contrôle de saisie ;
- **Projets & clients** : rentabilité, tâches, documents Sellsy et coûts par métier ;
- **Administration** : acquisitions incrémentales Odoo/Sellsy, contrôle distant
  en lecture seule, fraîcheur des onze sources, historique et recalcul local.

Les acquisitions et le recalcul sont deux opérations séparées. Une acquisition
publie une nouvelle révision de sa source ; le bouton **Actualiser les rapports**
reconstruit ensuite le rapport sans appeler Odoo ni Sellsy.

Les nouvelles révisions utilisent une base SQLite compressée par source. Les anciennes
révisions CSV/XLSX restent lisibles pendant la transition.

## Organisation du dépôt

| Emplacement | Responsabilité |
|---|---|
| `reporting/` | Interface web, serveur HTTP, agrégation et tests |
| `core/` | Connecteurs en lecture seule, révisions et historique technique |
| `data/` | Révisions locales et pointeur actif, exclus de Git |
| `tools/` | Démarrage, recalcul et déploiement local |
| `documentation/` | Architecture, règles de données et procédures d’exploitation |

Les secrets, les sources, les journaux et le rapport calculé sont exclus de Git.

## Documentation

- [Cartographie de l’architecture](documentation/architecture.md)
- [Sources, rapprochements et règles métier](documentation/donnees-et-regles.md)
- [Acquisition directe Odoo et Sellsy](documentation/acquisition-api-directe.md)
- [Exploitation locale et diagnostic](documentation/exploitation-locale.md)
- [Détails du composant Reporting](reporting/README.md)

## Validation

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s reporting\tests -p "test_*.py"
```

La commande `deploy_local.cmd` exécute cette suite avant tout recalcul et
redémarrage.
