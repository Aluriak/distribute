# échange-billet

Remplace une liste de diffusion utilisée au CHU de Brest pour échanger,
donner ou chercher des places de spectacle (point de départ : le spectacle
de Noël du CDC). Deux briques coexistent sur la même appli :

1. **échange-billet** — un marché vivant : les gens postent des annonces
   (don, échange, recherche, ou un simple dépôt sans contrepartie encore
   décidée), le système les apparie et les met en relation.
2. **répartition initiale** — un solveur d'allocation à instant T, pour
   distribuer équitablement les places dès le départ plutôt qu'au hasard.
   Son résultat, une fois publié, devient le point de départ du marché
   d'échange (chaque personne servie reçoit une annonce "dépôt").

Ce document résume l'architecture et les décisions de conception prises
au fil du développement, pour qu'une reprise (notamment par une session
Claude Code) n'ait pas à tout redécouvrir.

## Démarrage

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt   # nicegui + pyyaml + pulp 2.8 + pytest
python -m pytest                  # 49 tests, config dans pyproject.toml
```

Les tests ne touchent que `models.py` et `allocation.py` : ils tournent
sans `nicegui` ni `cdctools`. L'appli (`app.py`) a besoin en plus de
`cdctools` (module interne du CDC, fournissant `appacl`) et d'un
Keycloak configuré. La CI GitHub (`.github/workflows/tests.yml`) lance
la suite à chaque push.

## Fichiers

| Fichier | Rôle |
|---|---|
| `models.py` | Modèle de données + `Repo` (persistance SQLite) + **UN SEUL** moteur de matching (dons, bilatéral, cycles à N — cf. section dédiée). Aucune dépendance à NiceGUI — testable isolément. |
| `app.py` | L'appli NiceGUI (auth OIDC via `appacl`, fourni par `cdctools`, non inclus ici). |
| `cron.py` | Job quotidien (midi) : calcule le cycle de matching, envoie les notifications, valide automatiquement les échanges silencieux. À brancher sur un systemd timer. |
| `allocation.py` | Répartition initiale : tirage au sort pondéré (`run_final_draw`, `preview_probabilities`) — mécanisme utilisé par l'appli. Contient aussi `solve_egalitarian` (ILP via PuLP/CBC, minimise le pire rang), testée mais **plus utilisée en pratique** depuis que le tirage au sort l'a remplacée (cf. section dédiée plus bas). Réutilise `Slot` de `models.py` mais n'a pas d'autre dépendance au reste. |
| `notif.py` | **Stub** — journalise sur stdout. À remplacer par l'envoi réel (relais SMTP du CHU) avant mise en production. Interface à conserver : `notify(agents: list[str], body: str) -> None`. |
| `pyproject.toml` | Métadonnées du projet + configuration pytest (`testpaths`, `pythonpath`). |
| `tests/` | Suite pytest couvrant le moteur de matching unifié, le cycle Proposition→Transaction(s), `propose_exchange`, le tirage au sort, et le pipeline gestionnaire. `python -m pytest tests/ -v` depuis la racine. |

**Il n'y a plus de fichier `cycles.py`** : tout (recherche de cycles à N
parties comprise) vit maintenant dans `models.py`. C'était initialement
un module séparé, avec sa propre `CycleProposition` et son propre
registre de réservation — ça créait une vraie faille (une proposition de
cycle en attente était invisible du moteur bilatéral, qui pouvait
reproposer la même capacité ailleurs). La correction n'a pas été de
synchroniser deux comptabilités, mais de n'en garder qu'une seule : un
échange bilatéral N'EST QU'UN cycle de longueur 2, donc un seul moteur,
une seule `Proposition`, un seul registre.

## Modèle de données — vue d'ensemble

Une **annonce** (`Listing`) a un type (`echange` ou `recherche`), peut
offrir des places sur un créneau (`offered_slot` + `offered_count`), et/ou
en chercher via une liste de **groupes** (`wanted_groups`), chacun avec son
propre effectif et ses propres créneaux acceptés (ou aucun = wildcard,
"n'importe quel créneau validé"). Un groupe par `WantedGroup` permet de
scinder une demande entre plusieurs créneaux différents, chacun matché
indépendamment.

Quatre états pour une annonce d'échange, dérivés de `is_gift` et
`wanted_groups` (voir `listing_label()`) :

- **don** (`is_gift=True`) : gratuit, aucune réciprocité exigée.
- **dépôt** (`wanted_groups` vide, `is_gift=False`) : places déclarées,
  contrepartie pas encore décidée. N'est jamais matché automatiquement
  tant que personne n'a cliqué "je me propose" dessus (qui enrichit ses
  critères) ou que son auteur ne les a pas précisés manuellement.
- **échange** (`wanted_groups` non vide) : critères explicites, matching
  automatique si un donneur réciproque existe.
- **recherche** : aucune offre, juste une demande — ne peut être satisfaite
  que par un don (pas par un échange, qui exige une réciprocité).

### Le moteur unifié : don, bilatéral, cycle à N — un seul mécanisme

Un don (une seule relation, pas de retour), un échange bilatéral (A↔B,
soit un cycle fermé de longueur 2) et un cycle à N parties (A→B→C→...→A)
sont représentés par UNE seule `Proposition` générique : une liste
d'étapes (`steps`, chacune `(donneur_id, receveur_id, slot_key, count,
groupe_id)`) et un engagement individuel requis de chaque participant
(`accepted`). `Proposition.participant_ids` déduit l'ensemble des
annonces impliquées directement des étapes (union des donneurs et
receveurs) — pas de distinction de cas particulier pour N=1 ou N=2.

```
Listing(s) compatibles (don, bilatéral, ou cycle de longueur N)
         |
    [cycle quotidien, midi — cron.py : run_daily_matching_cycle]
         v
    Proposition (EN_ATTENTE, N participants, N >= 2)
         |  notifie TOUS les participants : chacun doit cliquer "je m'engage"
         v
    accepted_by_all ? ──non, un seul refuse──> REFUSEE (libère toute la capacité)
         |
         oui (revalidation défensive des capacités — cf. plus bas)
         v
    N Transaction (PROPOSEE) — une par étape, chacune suit INDÉPENDAMMENT :
         |
    confirm / dispute (les deux côtés DE CETTE ÉTAPE doivent confirmer)
         |
    un seul confirme, l'autre silencieux > N jours ──> REALISEE (auto_side posé)
         |                                                  |
         |                                    contestable (fenêtre de grâce)
         v                                                  v
      LITIGE <───────────────────────────────────────── rouvert
         |
    un modérateur tranche (REALISEE ou ANNULEE)
```

**Un don produit 1 Transaction, un bilatéral en produit 2 (A→B et B→A
séparément — plus de champ "réciproque" unique), un cycle à N en produit
N.** Chaque Transaction suit le même cycle confirm/dispute indépendamment
des autres : si, dans un cycle à 3, l'étape B→C échoue (litige, annulée),
les étapes A→B et C→A ne sont pas automatiquement affectées — c'est plus
précis que l'ancien modèle bilatéral, où confirmer "l'échange" validait
les deux sens d'un coup sans distinguer lequel avait réellement eu lieu.

La recherche elle-même se fait en deux passes qui partagent le même
registre de réservation (`remaining_offered`/`remaining_group`, mutées
en continu) :
1. `_match_gifts` — correspondance directe don → chercheur, aucune
   réciprocité exigée (un don ne peut jamais faire partie d'un cycle
   fermé : il n'attend rien en retour, donc n'a pas de "pointeur sortant"
   dans l'algorithme Top Trading Cycles).
2. `find_exchange_cycles` — cycles fermés de longueur 2 (bilatéral,
   systématique) jusqu'à `max_cycle_len` (cycles à N parties, seulement
   si le gestionnaire a relevé la longueur — cf. plus bas). Même
   algorithme pour les deux : un bilatéral est juste un cycle trouvé de
   longueur 2.

`accept_proposition_step` revalide les capacités (hors réservation de la
proposition elle-même, pour éviter l'auto-blocage) au moment où le
DERNIER participant accepte, et fait échouer proprement la proposition
(`ECHOUEE`) si elles ne suffisent plus. Comme il n'existe plus qu'un seul
registre de réservation, ce n'est plus un filet de sécurité contre une
faille connue (deux comptabilités qui s'ignoraient) — c'est redevenu un
simple garde-fou contre des cas limites authentiquement rares (annonce
close entre-temps par une action sans lien, écriture concurrente sous
WAL). Couvert par `test_le_registre_partage_protege_un_cycle_en_attente`.

### Cycles à N parties : gouvernance

Le gestionnaire **ne valide jamais** un échange, bilatéral ou à N
parties — il règle uniquement la longueur maximale recherchée par le
cycle quotidien automatique (`/gestion`), qui notifie alors lui-même
tous les participants concernés. Chaque participant doit ensuite
s'engager individuellement dans `/mes-echanges` (bouton qui rappelle
explicitement, si N > 2, que les N-1 autres comptent sur lui) ; le refus
d'un seul défait toute la proposition.

Deux niveaux pour cette longueur maximale :
- un **plafond de déploiement**, dans `config.yml`
  (`max_cycle_length_ceiling`, défaut 2 = cycles à N parties désactivés,
  seul le bilatéral — longueur 2 — tourne) ;
- une **valeur runtime** choisie par le gestionnaire dans `/gestion`
  (`Repo.set_max_cycle_length`), qui ne peut que réduire ce plafond,
  toujours strictement entre 3 et le plafond (jamais 2, qui ne ferait
  que redécouvrir les paires bilatérales déjà traitées par la même
  recherche).

### Répartition initiale : tirage au sort pondéré (`allocation.py`)

Mécanisme retenu : **pas** un solveur déterministe — un vrai tirage au
sort pondéré (Random Serial Dictatorship). Chaque participant reçoit une
clé aléatoire `U^(1/poids)` (Efraimidis-Spirakis) ; triés par clé
décroissante, ça donne un ORDRE DE TIRAGE où un poids plus élevé
(`priority_weight`, hérité de l'historique inter-éditions) donne une
meilleure ESPÉRANCE de passer tôt, sans jamais la garantir. Dans cet
ordre, chacun prend ensuite la meilleure option encore disponible parmi
ses préférences (attribution sérielle).

**Le tirage ne "remplace" la résolution que là où il y a contention.**
Ce n'est pas deux étapes séparées ("résolution déterministe, puis loterie
seulement en cas d'égalité") mais un seul mécanisme intégré qui a cette
propriété par construction : sur un créneau sans tension, l'ordre de
tirage ne change rien (100% de chances pour tout le monde, quel que soit
cet ordre) ; l'aléa ne devient déterminant exactement là où la demande
dépasse l'offre. Conséquence directe : personne ne peut jamais "gagner"
deux créneaux à la fois (même si ses deux premiers choix sont tous les
deux très demandés) — l'attribution sérielle s'arrête au premier succès
de chacun. Démontré par
`test_jamais_deux_creneaux_meme_si_les_deux_premiers_choix_sont_tendus`
et `test_creneau_non_dispute_toujours_garanti_quel_que_soit_lordre`.

Deux usages distincts de ce même mécanisme :
- `preview_probabilities` — pendant la collecte, simule le tirage des
  centaines de fois avec des graines aléatoires à chaque fois, et renvoie
  la probabilité empirique, pour chaque participant, d'obtenir chacun des
  créneaux demandés (100% si non disputé). Ne persiste rien, rejouable à
  volonté.
- `run_draw_iteration` / `run_final_draw` — un tirage CANDIDAT, avec
  justification obligatoire, peut être relancé autant de fois que
  nécessaire (`/gestion`) : chaque itération est intégralement enregistrée
  (graine, ordre tiré, résultat, qui, quand, pourquoi) et **visible par
  tout le monde** sur `/historique-tirages`, publiée ou non. Les graines
  successives d'une même édition sont dérivées d'une même base (seul le
  numéro d'itération change dans les derniers chiffres, cf.
  `_derive_seed`) — pas des graines arbitraires sans lien entre elles.
  Seule la PUBLICATION (`Repo.mark_published`, qui enregistre aussi
  combien d'itérations ont précédé) reste irréversible : `is_published`
  bloque tout nouveau tirage une fois appelée.

**Point critique à ne jamais régresser** : `active_transactions()` compte
PROPOSEE, LITIGE *et* REALISEE comme "consommant de la capacité" — seule
ANNULEE la libère. Une version antérieure ne comptait que PROPOSEE/LITIGE,
ce qui permettait à un ticket déjà réellement échangé (REALISEE) d'être
re-proposé à quelqu'un d'autre lors du cycle suivant. Couvert par
`test_transaction_realisee_ne_libere_jamais_la_capacite`.

### Fichier de config (`CONFIG_DIR/config.yml`, sur le modèle de `skel`)

```yaml
max_cycle_length_ceiling: 4   # 2 = cycles à N parties désactivés (défaut)
moderateurs:
  - mod1@chu-brest.fr
  - mod2@chu-brest.fr
```

`moderateurs` sert à notifier directement les modérateurs quand un litige
s'ouvre (dispute ou contestation) — pas d'appel à l'API d'administration
Keycloak, la liste est maintenue à la main dans ce fichier.

### Rôles (Keycloak, noms configurables par variable d'environnement)

- **extérieur** : aucun rôle applicatif (informatif, non vérifié
  activement — c'est juste "ni utilisateur ni modérateur").
- **utilisateur** : poste, consulte, répond aux propositions.
- **modérateur** : valide les créneaux saisis librement par les
  utilisateurs, tranche les litiges.
- **gestionnaire** : **indépendant** de l'échelle précédente (pas un
  niveau au-dessus — on l'a souvent EN PLUS, jamais automatiquement) :
  seul lui définit les créneaux du spectacle et leur capacité, et
  déclenche/relance le calcul de répartition.

## Déploiement

Variables d'environnement principales (toutes avec une valeur par défaut
dans le code, voir le haut de `app.py` et `cron.py`) :

- `SHOW_ID`, `SHOW_LABEL` — identifiant et libellé du spectacle courant.
  **Un `SHOW_ID` par édition** (ex. `noel-2025`, `noel-2026`) — c'est ce
  qui permet à l'historique inter-éditions de fonctionner.
- `ECHANGE_BILLET_DB` — chemin du fichier SQLite, **partagé entre l'appli
  web et `cron.py`** (volume commun si conteneurisé séparément).
- `ROLE_UTILISATEUR`, `ROLE_MODERATEUR`, `ROLE_GESTIONNAIRE` — noms des
  rôles Keycloak.
- `AUTO_VALIDATE_DAYS` (cron.py), `CONTEST_GRACE_DAYS` (app.py) — délais
  du cycle confirm/dispute/auto-validation/contestation.
- `OIDC_CLIENT_ID` — client Keycloak (passé à `appacl.set()`).

`cron.py` est pensé pour un systemd timer quotidien à midi (sur le modèle
de `probastack.py`) — indépendant du processus NiceGUI, importe
`models`, `notif`, et lit le même `CONFIG_DIR/config.yml` qu'`app.py`
(`max_cycle_length_ceiling`) : **ce plafond doit rester identique des
deux côtés**, sans quoi le cron pourrait chercher des cycles plus longs
(ou plus courts) que ce que l'appli affiche au gestionnaire.

Le fichier SQLite tourne en mode WAL (`PRAGMA journal_mode=WAL`), pour
limiter les erreurs "database is locked" sous écriture concurrente entre
l'appli web et le cron.

## Ce qui reste ouvert

- **Republier une édition après une correction.** Une fois
  `mark_published()` appelé (répartition initiale), plus aucune
  republication n'est possible (protection anti-doublon). Si le
  gestionnaire doit corriger une erreur après publication, il n'y a
  aujourd'hui aucun mécanisme prévu — à construire si le besoin se
  présente. Même remarque pour un cycle à N parties publié : "ne peut
  plus être changé" est un choix assumé (pas d'undo dans l'appli ; une
  correction passerait par une intervention directe en base).
- **`compute_priority_weight` est une formule de départ** (`1 + moyenne
  des rangs passés`) — isolée et testée indépendamment, donc facile à
  retravailler si l'usage réel suggère une courbe différente.
- **Pas de pré-remplissage du formulaire `/preferences`** si l'utilisateur
  a déjà soumis une préférence — il repart de zéro pour la modifier,
  simplification assumée.
- **Performance de `eligible_listings_for`** (page d'accueil) : une
  requête par annonce affichée pour calculer le bouton "je me propose" —
  largement suffisant pour l'échelle actuelle (un spectacle, quelques
  dizaines à centaines d'annonces), mais à revoir si le volume grandit
  beaucoup (plusieurs spectacles en parallèle, cf. le champ `show_id` déjà
  générique).
- **`notif.py` est un stub.** À remplacer par l'intégration réelle avant
  toute mise en production — sans quoi aucune notification ne part
  réellement (juste un log).
- **La liste des modérateurs (`config.yml`) est maintenue à la main.**
  Si elle devient difficile à synchroniser avec les rôles Keycloak
  réellement attribués, un appel à l'API d'administration Keycloak pour
  la dériver automatiquement serait plus robuste.
