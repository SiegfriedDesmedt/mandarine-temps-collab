# Acquisition directe Sellsy / Odoo

## Flux actif

`Administration -> acquisition Odoo ou Sellsy -> validation de la revision -> actualisation locale des rapports`

- Odoo est lu par XML-RPC avec une liste fermee de methodes de lecture.
- Sellsy est lu par API V2 OAuth2 sur les commandes, factures et avoirs.
- Chaque acquisition cree une revision immuable dans
  `data/source-revisions/<source>/<revision>/`.
- `data/active_sources.json` reference independamment les revisions actives
  Odoo et Sellsy.
- Une acquisition ne remplace que sa propre source et ne recalcule jamais le
  rapport.
- En cas d'echec, les revisions actives et le rapport precedent restent
  inchanges.
- L'actualisation des rapports ne declenche aucun appel reseau.

## Sources des rapports

Odoo fournit huit fichiers CSV : clients, commandes, lignes de commande,
projets, taches, temps, couts fixes et collaborateurs.

Sellsy fournit trois classeurs XLSX : commandes, factures et avoirs. Les
documents sont limites a partir du 1er janvier 2021 et les factures conservent
la relation technique vers leur commande parente lorsqu'elle est disponible.

Le reporting ne collecte ni detail de lignes destine a la creation de devis,
ni copie JSON compressee des sources.

## Conservation locale

Les donnees sont exclues de Git. La politique locale conserve la revision
active et une revision precedente valide pour chaque source. Les manifestes
contiennent la taille et l'empreinte SHA-256 des fichiers necessaires.
