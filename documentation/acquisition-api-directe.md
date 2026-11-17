# Acquisition directe Odoo et Sellsy

## Principe

Les deux acquisitions sont indépendantes, manuelles et strictement orientées
lecture :

```text
API distante -> delta borné -> copie/migration SQLite en staging -> validation
             -> manifeste -> promotion atomique
```

Une acquisition réussie remplace uniquement le pointeur actif de sa source.
Elle ne reconstruit pas `reporting/dist/data.json` et ne modifie pas la révision
active de l’autre système.

## Acquisition Odoo

Le connecteur cible actuellement l’instance et la base déclarées dans
`core/odoo_exporter.py`. Il utilise XML-RPC et limite les méthodes objets à
`fields_get`, `search`, `search_read`, `read` et `search_count`.

Les huit jeux de données logiques suivants sont conservés dans `odoo.sqlite.gz` :

| Table logique (ancien fichier) | Origine |
|---|---|
| `clients.csv` | Sociétés `res.partner` |
| `commandes.csv` | Commandes confirmées `sale.order` |
| `lignes_commandes.csv` | `sale.order.line` |
| `projets.csv` | `project.project` actifs et archivés |
| `taches.csv` | `project.task` |
| `temps.csv` | `account.analytic.line` |
| `couts_fixes.csv` | Écritures des six produits de coûts reconnus |
| `collaborateurs.csv` | Employés, utilisateurs et départements associés |

Les projets et tâches n’exposant pas directement l’employé attendu, le
connecteur dérive `employee_id` par la relation technique unique avec
`hr.employee.user_id`. Les schémas et champs obligatoires sont vérifiés avant
publication.

Après une première acquisition complète, le connecteur utilise `write_date`
avec une fenêtre de recouvrement pour ne relire que les lignes nouvelles ou
modifiées. Les lignes sont fusionnées par identifiant Odoo avec un UPSERT. Les
collaborateurs, peu volumineux et nécessaires aux relations utilisateur/employé,
restent relus intégralement. Une synchronisation complète périodique réconcilie
les suppressions et les changements qui font sortir une ligne de son domaine
Odoo, par exemple une commande qui n'est plus confirmée.

L'administration permet aussi de déclencher cette opération immédiatement avec
**Réconciliation complète**. Cette demande omet volontairement le filtre
`write_date`, reconstruit la base Odoo à partir du résultat exhaustif, puis publie
la nouvelle révision de façon atomique. Elle ne recalcule pas automatiquement le
rapport : l'action **Actualiser les rapports** reste une étape distincte.

## Acquisition Sellsy

L’authentification utilise OAuth2 `client_credentials`. Deux sociétés peuvent
être lues : le Sellsy principal fournit commandes, factures et avoirs ; la
connexion optionnelle Mandarine Expérience complète uniquement les commandes.
Les recherches V2 sont
limitées à :

- `/orders/search` ;
- `/invoices/search` ;
- `/credit-notes/search`.

Le client autorise aussi la lecture des livraisons, sans l’utiliser dans le flux
nominal actuel. Les sous-ressources de lecture servent aux champs personnalisés. Une
lecture V1 en liste fermée (`Document.getList`, `Document.getLinkedDocuments`,
`Document.getOne`) peut reconstruire le lien entre un bon de livraison et sa
commande parente. Aucun endpoint de mutation n’est exposé.

Trois tables sont conservées dans `sellsy.sqlite.gz` : `sellsy_orders`,
`sellsy_invoices` et `sellsy_credit_notes`. Elles contiennent notamment les identifiants, numéros, dates,
clients, montants HT, statuts, champs BU/projet et relation de commande parente.
Les identifiants de commandes Mandarine Expérience sont placés dans un espace
numérique distinct afin d’éviter toute collision avec ceux du Sellsy principal.
La première acquisition qui suit l’activation de cette seconde connexion force
une réconciliation complète afin de récupérer aussi les commandes historiques,
puis les acquisitions suivantes redeviennent incrémentales.
Le périmètre commence au 1er janvier 2021 et la pagination V2 est limitée à 100
éléments par page jusqu’à épuisement.

Après la première acquisition, le filtre documenté `updated` limite la recherche
aux documents modifiés depuis le dernier curseur validé, avec recouvrement. Les
champs personnalisés ne disposent pas dans ce flux d'une récupération groupée
fiable par code : ils sont donc relus uniquement pour les commandes et avoirs
nouveaux ou modifiés, puis conservés dans SQLite. La première acquisition
complète reste par conséquent la plus longue. Cette optimisation suppose que
Sellsy actualise le marqueur `updated` lorsqu'un champ personnalisé change ; la
réconciliation complète périodique couvre le cas contraire.

Le bouton **Réconciliation complète** de l'administration permet de déclencher
immédiatement cette relecture exhaustive pour le Sellsy principal et les commandes
Mandarine Expérience. Aucun curseur `updated` n'est envoyé pendant cette opération.

Le client réessaie temporairement les réponses `429`, `500`, `502`, `503` et
`504`, respecte `Retry-After` et force IPv4 lorsque la route IPv6 Windows est
inutilisable.

## Révision et manifeste

Chaque lot validé est publié sous :

```text
data/source-revisions/<source>/<source>-<horodatage>-<identifiant>/
```

Le `manifest.json` contient :

- la version de schéma et le système source ;
- l’identifiant et les heures de début/fin ;
- le nombre total d’enregistrements ;
- la liste des fichiers, leur taille et leur SHA-256 ;
- les contrôles de complétude, pagination et lecture seule.
- le format SQLite compressé et sa version de schéma, ainsi que les volumes par table ;
- le mode `full` ou `incremental`, le curseur, la fenêtre de recouvrement et la
  date de la dernière réconciliation complète.

La promotion vérifie le manifeste et chaque empreinte requise, déplace le
staging vers son emplacement définitif, puis remplace atomiquement
`data/active_sources.json`. Si l’écriture du pointeur échoue, la nouvelle
révision est retirée et l’ancienne reste active.

Le curseur n'est donc jamais avancé par une simple réponse API : il ne devient
actif qu'avec le manifeste de la révision promue. Les révisions historiques en
CSV/XLSX restent lisibles et sont migrées dans le staging lors de la première
synchronisation incrémentale SQLite.

## Échecs et confidentialité

- un lot incomplet n’est jamais activé ;
- les répertoires de staging sont nettoyés après succès ou échec ;
- les secrets sont masqués dans les erreurs connues ;
- l’historique conserve le statut, les étapes, volumes, durées et erreurs, mais
  pas les identifiants ni les secrets ;
- aucune extraction automatique n’a lieu au démarrage et aucun ordonnanceur
  n’est configuré.

Par défaut, une réconciliation complète est imposée tous les sept jours. Entre
deux réconciliations, les suppressions distantes peuvent rester visibles
localement ; cette limite est explicite dans le manifeste.

## Mesure locale de référence

Une conversion temporaire des révisions actives du 13 novembre 2026, sans appel
réseau, a donné les résultats suivants :

| Source | Format historique | SQLite.gz | Gain mesuré |
|---|---:|---:|---:|
| Odoo, 207 059 lignes | 23 888 874 octets | 5 487 324 octets | 77,0 % |
| Sellsy, 4 043 documents | 412 892 octets | 169 250 octets | 59,0 % |

Les sections `summary`, `monthlyBu`, `finance`, `collaborators`, `projects`,
`alerts` et `controls` produites par les deux formats étaient identiques. Sur un
passage local, le calcul a pris 8,59 s avec les fichiers historiques et 8,29 s
avec SQLite.gz. Ces durées ne mesurent pas les API ; le gain de synchronisation
incrémentale doit être observé lors d'une exécution contrôlée.

La rétention des anciennes révisions n’est pas automatique. Voir le
[guide d’exploitation](exploitation-locale.md#sauvegarde-et-rétention).
