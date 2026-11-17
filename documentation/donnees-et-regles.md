# Sources, rapprochements et règles métier

## Catalogue des onze sources

| Source logique | Système / modèle | Table (ancien fichier) | Usage principal | Fraîcheur attendue |
|---|---|---|---|---:|
| Clients | Odoo `res.partner` sociétés | `clients.csv` | Client final des projets | 24 h |
| Commandes | Odoo `sale.order` à l’état `sale` | `commandes.csv` | Référence Sellsy et relations projet | 24 h |
| Lignes de commande | Odoo `sale.order.line` | `lignes_commandes.csv` | Montants et relations tâche/projet | 24 h |
| Projets | Odoo `project.project` | `projets.csv` | Référentiel des projets | 24 h |
| Tâches | Odoo `project.task` | `taches.csv` | Budget courant et consommation | 24 h |
| Temps | Odoo `account.analytic.line` | `temps.csv` | Heures et coût de main-d’œuvre | 24 h |
| Coûts fixes | Odoo `account.analytic.line` filtré | `couts_fixes.csv` | Coûts hors main-d’œuvre | 24 h |
| Collaborateurs | Odoo employés/utilisateurs/départements | `collaborateurs.csv` | Identité et rattachement métier | 24 h |
| Commandes | Sellsy | `orders.xlsx` | Commandé HT et profil BU | 48 h |
| Factures | Sellsy | `invoices.xlsx` | Chiffre d’affaires facturé | 48 h |
| Avoirs | Sellsy | `credit-notes.xlsx` | Déduction du chiffre d’affaires | 48 h |

Les nouvelles révisions regroupent ces tables dans `odoo.sqlite.gz` et
`sellsy.sqlite.gz`. Les noms CSV/XLSX du tableau restent ceux des révisions
historiques compatibles. Les dates de fraîcheur reposent sur la modification du fichier actif. Elles
indiquent un état opérationnel, pas la date métier maximale contenue dans la
source.

## Périmètre temporel

- documents Sellsy : à partir du **1er janvier 2021** ;
- exercices affichés : juillet à juin, à partir de **2021–2022** ;
- lignes postérieures à la date du calcul : exclues des agrégats ;
- les relations projet et les cumuls historiques restent disponibles en dehors
  de l’exercice sélectionné dans les fiches projet.

## Rapprochement commandes et projets

1. Le numéro de commande Sellsy est normalisé sans supprimer les zéros initiaux.
2. Il est comparé aux références de `sale.order.x_num_sellsy`. Plusieurs
   références Odoo peuvent être séparées par `|`, `;`, `,` ou un saut de ligne.
3. Une égalité technique unique est obligatoire ; aucun rapprochement par nom
   de client n’est effectué.
4. Les projets candidats sont collectés depuis la commande, ses lignes, ses
   tâches et les relations commande/ligne portées par le projet.
5. Un seul projet candidat doit subsister. Une absence, un doublon ou une
   ambiguïté reste dans les contrôles sans affectation forcée.

Les commandes Sellsy non rapprochées restent incluses dans la synthèse globale,
mais n’alimentent aucune fiche projet.

## Factures et avoirs

- Une facture est affectée par sa **commande parente native Sellsy**. Si son
  parent est un bon de livraison, une lecture Sellsy V1 autorisée reconstruit le
  lien livraison → commande.
- Une facture sans commande parente ou dont la commande n’est pas rapprochée
  reste non affectée.
- Un avoir utilise le champ personnalisé Sellsy `project` uniquement lorsque
  cette clé correspond, via les commandes déjà rapprochées, à un projet Odoo
  unique.
- Les documents annulés sont exclus des montants financiers.

## Calculs financiers

| Indicateur | Règle |
|---|---|
| Commandé HT | Somme des commandes Sellsy hors brouillons et annulations |
| Facturé HT | Somme des factures Sellsy non annulées |
| Avoirs HT | Valeur absolue des avoirs non annulés |
| CA net | Facturé HT − avoirs HT |
| Coût main-d’œuvre | Opposé du montant analytique Odoo des lignes de temps |
| Autres coûts | Écritures analytiques des produits de coûts fixes |
| Coût total | Main-d’œuvre + autres coûts |
| Reste à facturer | Commandé HT − CA net |
| Marge main-d’œuvre | CA net − coût main-d’œuvre |
| Marge opérationnelle | CA net − coût total |

Dans le tableau **Portefeuille projets**, la colonne **Facturé net HT** affiche
le CA net, donc les factures diminuées des avoirs rapprochés. Le détail brut
des factures et des avoirs reste disponible dans la fiche du projet et dans
l’export.

Les catégories de coûts fixes reconnues sont `COUT-ACQ`, `COUT-FRAIS`,
`COUT-INT`, `COUT-MWIZ`, `COUT-LIC` et `COUT-EXT`.

## Profil des projets

- Un projet est **Interne** si son nom contient `INTERNE`, si le nom de son
  client Odoo contient `Mandarine`, ou si son champ Odoo `x_bu_projet` vaut
  `MANDARINE INTERNE`. Dans tous les autres cas, il est **Client**.
- La BU provient en priorité des commandes Sellsy rapprochées, pondérées par le
  montant absolu. En l’absence de commande, une règle de repli utilise le nom du
  projet.
- Le budget d’une tâche correspond au `planned_hours` courant d’Odoo. Il ne
  constitue pas un historique des révisions de budget.
- Le statut de consommation d’une tâche est : sans budget, normal sous 90 %, à
  surveiller de 90 à 100 %, puis dépassé au-delà de 100 %.

## Contrôle mensuel des temps

La capacité de référence est globale : **7 h 40 par jour de semaine**. Elle ne
tient pas compte des jours fériés ni des contrats individuels. La période est
proratisée avec la date du calcul et les premières/dernières activités connues.
Les absences identifiées comptent comme temps saisi.

| Couverture | État |
|---:|---|
| `< 85 %` | Insuffisant |
| `85 % à < 95 %` | À surveiller |
| `95 % à 108 %` | Conforme |
| `> 108 %` | À surveiller |

Les porteurs de coûts historiques identifiés par ID ou libellé ne sont pas
présentés comme collaborateurs ; leurs montants restent visibles dans les
contrôles et les fiches projet.

## Contrat du rapport publié

`reporting/dist/data.json` utilise actuellement `reportSchemaVersion = 4` et
`rulesetVersion = 4`.

| Section | Contenu |
|---|---|
| `meta` | Périodes, date de calcul, provenance et règles appliquées |
| `summary` | Indicateurs consolidés de l’exercice courant |
| `monthlyBu` | Heures et montants mensuels par BU |
| `finance` | Ventilation des factures par statut |
| `collaborators` | Collaborateurs et contrôles mensuels |
| `projects` | Fiches projet complètes et séries mensuelles |
| `sources` | Métadonnées des onze sources logiques |
| `alerts` | Points de vigilance affichés dans la synthèse |
| `controls` | Compteurs de cohérence sans listes métier détaillées |

Avant publication, le générateur vérifie la présence des sections, le nombre de
sources, la sérialisation JSON et l’absence de valeurs non finies. Les onze
fichiers physiques SQLite ou historiques sont empreintés avant et après calcul avec leur taille, date de
modification et SHA-256 ; toute variation annule la publication.

## Contrôles conservés

- lignes de temps sélectionnées et écritures analytiques non assimilées à du temps ;
- temps sans coût, projet ou tâche ;
- temps futurs ;
- commandes non rapprochées et montant concerné ;
- taux de présence des références Sellsy dans Odoo ;
- projets avec temps sans relation vers une commande ;
- porteurs de coûts historiques et anomalies de rattachement des coûts fixes.
