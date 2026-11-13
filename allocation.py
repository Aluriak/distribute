"""Solveur de répartition initiale des billets — projet complémentaire à
échange-billet, mais qui en réutilise le modèle de créneaux (Slot).

Contrairement à échange-billet (marché vivant, les gens négocient après
coup), ici on résout tout en une fois, à un instant T, avant toute
distribution : chacun exprime ses préférences, un solveur calcule
l'attribution.

MÉCANISME RETENU : un tirage au sort pondéré (Random Serial Dictatorship),
PAS un solveur déterministe — cf. `run_final_draw` / `preview_probabilities`
plus bas. `solve_egalitarian` (ILP, minimise le pire rang) reste dans ce
module, testée, mais n'est plus le mécanisme utilisé par l'appli : un
optimum déterministe ne peut pas répondre à "quelle est ma probabilité
d'obtenir ce créneau", et masque arbitrairement les cas à égalité derrière
le choix interne du solveur plutôt que de les trancher par un vrai hasard,
auditable.

Hypothèse de modélisation, commune aux deux mécanismes : les `count`
places d'un même participant sont attribuées EN BLOC à un seul créneau
(pas de scission d'un groupe entre deux séances) — à l'inverse
d'échange-billet où scinder est permis.
"""
from __future__ import annotations

import json
import random
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pulp

from models import Slot, Repo


@dataclass
class Participant:
    id: str              # identifiant stable (email)
    name: str
    count: int            # nombre de places souhaitées ; 0 => ne veut rien (exclu du calcul)
    # Niveaux de préférence, du meilleur au pire. Un niveau est une liste de
    # créneaux acceptés à ce rang (indifférent entre eux) ; une liste VIDE à
    # un rang donné signifie "n'importe quel créneau restant" (le "je m'en
    # fiche" couvre tout, un classement partiel peut aussi finir par un
    # niveau vide pour dire "sinon, peu importe, parmi ce qui reste").
    tiers: list[list[Slot]]
    # Historique (facultatif) : plus la valeur est grande, plus le rang
    # obtenu CETTE édition pèse lourd dans l'objectif — typiquement > 1
    # pour quelqu'un mal servi lors d'éditions précédentes, pour que le
    # solveur fasse un effort particulier pour ne pas l'infliger deux fois.
    priority_weight: float = 1.0


@dataclass
class SlotSupply:
    slot: Slot
    capacity: int


@dataclass
class Assignment:
    participant_id: str
    participant_name: str
    slot: Optional[Slot]   # None si non servi
    count: int
    tier_index: Optional[int]   # rang obtenu (0 = meilleur choix) ; None si non servi


def _resolve_tiers(participant: Participant, all_slots: list[Slot]) -> dict[str, tuple[Slot, int]]:
    """Pour chaque créneau, le MEILLEUR rang (le plus petit index) auquel ce
    participant l'accepterait, en résolvant les niveaux "n'importe quel
    créneau" (liste vide) sur les créneaux pas encore couverts par un niveau
    plus explicite et plus favorable."""
    resolved: dict[str, tuple[Slot, int]] = {}
    for tier_index, tier_slots in enumerate(participant.tiers):
        candidates = tier_slots if tier_slots else all_slots
        for slot in candidates:
            if slot.key() not in resolved:
                resolved[slot.key()] = (slot, tier_index)
    return resolved


def default_unserved_penalty(participants: list[Participant]) -> int:
    """La valeur de pénalité qu'utilisera solve_egalitarian pour cette liste
    de participants — exposée pour que l'appelant puisse calculer les poids
    d'historique (compute_priority_weight) AVANT l'appel, avec la même
    échelle que celle réellement utilisée par le solveur."""
    actifs = [p for p in participants if p.count > 0]
    return max((len(p.tiers) for p in actifs), default=0)


def compute_priority_weight(historique: list[Optional[int]], unserved_penalty: int) -> float:
    """Transforme l'historique des rangs obtenus par une personne lors des
    éditions précédentes (None = non servie cette année-là) en poids de
    priorité pour l'édition en cours. Formule volontairement simple et
    transparente, à ajuster si l'usage réel le suggère :

        poids = 1 + moyenne(rangs passés, non-servi compté comme `unserved_penalty`)

    Pas d'historique -> poids neutre (1.0). Un mauvais rang isolé pèse peu
    (la moyenne l'amortit) ; une mauvaise série pèse de plus en plus —
    volontaire, pour viser l'équité sur la durée plutôt qu'édition par
    édition."""
    if not historique:
        return 1.0
    valeurs = [h if h is not None else unserved_penalty for h in historique]
    return 1.0 + sum(valeurs) / len(valeurs)


def solve_egalitarian(
    participants: list[Participant], supply: list[SlotSupply],
) -> tuple[list[Assignment], int]:
    """Retourne (assignations, pire rang obtenu parmi tous les participants
    servis — ou la valeur de pénalité si au moins un participant n'a pu être
    servi du tout). Un participant avec count=0 est ignoré (pas d'assignation)."""
    actifs = [p for p in participants if p.count > 0]
    all_slots = [s.slot for s in supply]
    capacity = {s.slot.key(): s.capacity for s in supply}

    if not actifs:
        return [], 0

    penalite_non_servi = max((len(p.tiers) for p in actifs), default=0)

    prob = pulp.LpProblem("repartition_billets", pulp.LpMinimize)
    z = pulp.LpVariable("pire_rang", lowBound=0, cat="Continuous")

    # variables binaires par (participant, créneau autorisé pour lui)
    vars_by_participant: dict[str, list[tuple[pulp.LpVariable, int, Slot]]] = {}
    slot_vars: dict[str, list[tuple[pulp.LpVariable, int]]] = {k: [] for k in capacity}  # slot_key -> [(var, count)]

    for p in actifs:
        resolved = _resolve_tiers(p, all_slots)
        entries = []
        for slot_key, (slot, tier_index) in resolved.items():
            if slot_key not in capacity:
                continue  # créneau non proposé par l'organisateur -> ignoré
            var = pulp.LpVariable(f"x_{p.id}_{slot_key}", cat="Binary")
            entries.append((var, tier_index, slot))
            slot_vars[slot_key].append((var, p.count))
        vars_by_participant[p.id] = entries

    for p in actifs:
        entries = vars_by_participant[p.id]
        non_servi = pulp.LpVariable(f"non_servi_{p.id}", cat="Binary")
        # exactement une option retenue : un créneau, ou "non servi"
        prob += pulp.lpSum(var for var, _, _ in entries) + non_servi == 1
        # le rang réellement vécu par ce participant (0 si son meilleur choix,
        # ..., ou la pénalité si non_servi) ne doit jamais dépasser z — pondéré
        # par sa priorité : à rang égal, quelqu'un de poids 2 "coûte" deux
        # fois plus cher dans l'objectif, donc le solveur l'épargne en premier
        rang_vecu = pulp.lpSum(var * tier_index for var, tier_index, _ in entries) + non_servi * penalite_non_servi
        prob += z >= p.priority_weight * rang_vecu

    for slot_key, entries in slot_vars.items():
        if entries:
            prob += pulp.lpSum(var * count for var, count in entries) <= capacity[slot_key]

    prob += z  # objectif : minimiser le pire rang vécu, tout le monde confondu
    prob.solve(pulp.PULP_CBC_CMD(msg=0))

    if pulp.LpStatus[prob.status] != "Optimal":
        raise RuntimeError(f"Le solveur n'a pas trouvé de solution ({pulp.LpStatus[prob.status]}).")

    assignments: list[Assignment] = []
    for p in actifs:
        chosen = next(((slot, tier_index) for var, tier_index, slot in vars_by_participant[p.id]
                        if var.value() == 1), None)
        if chosen:
            slot, tier_index = chosen
            assignments.append(Assignment(p.id, p.name, slot, p.count, tier_index))
        else:
            assignments.append(Assignment(p.id, p.name, None, p.count, None))

    pire_rang = int(round(z.value()))
    return assignments, pire_rang


# --------------------------------------------------------------------------
# Tirage au sort pondéré (Random Serial Dictatorship) — mécanisme retenu
# --------------------------------------------------------------------------

@dataclass
class DrawResult:
    seed: int
    draw_order: list[str]            # ids des participants, dans l'ordre tiré (servis en premier d'abord)
    assignments: list[Assignment] = field(default_factory=list)


def weighted_draw_order(participants: list[Participant], seed: int) -> list[str]:
    """Ordre de tirage pondéré (Efraimidis-Spirakis) : chaque participant
    reçoit une clé U^(1/poids) avec U tiré uniformément : plus le poids
    (`priority_weight`, hérité de l'historique) est grand, meilleure est
    l'ESPÉRANCE de passer tôt — mais ça reste un tirage, jamais une
    garantie. Trié par ordre décroissant de clé.

    Le tri initial des participants par id, avant de tirer les clés,
    garantit que le même `seed` donne TOUJOURS le même résultat quel que
    soit l'ordre dans lequel la base de données les a renvoyés —
    indispensable pour la reproductibilité."""
    rng = random.Random(seed)
    actifs = sorted((p for p in participants if p.count > 0), key=lambda p: p.id)
    keyed = []
    for p in actifs:
        u = rng.random()
        poids = max(p.priority_weight, 1e-6)
        keyed.append((u ** (1.0 / poids), p.id))
    keyed.sort(key=lambda kp: kp[0], reverse=True)
    return [pid for _, pid in keyed]


def allocate_by_draw_order(
    participants: list[Participant], supply: list[SlotSupply], draw_order: list[str],
) -> list[Assignment]:
    """Attribution sérielle, strictement déterministe une fois l'ordre de
    tirage connu : dans cet ordre, chacun prend la meilleure option
    (son rang le plus favorable) encore disponible en capacité suffisante
    pour la totalité de son `count` (pas de scission, cf. docstring du
    module)."""
    by_id = {p.id: p for p in participants}
    all_slots = [s.slot for s in supply]
    remaining = {s.slot.key(): s.capacity for s in supply}

    assignments: list[Assignment] = []
    for pid in draw_order:
        p = by_id[pid]
        resolved = _resolve_tiers(p, all_slots)
        candidats = sorted(resolved.items(), key=lambda kv: kv[1][1])  # par rang croissant
        chosen = next(
            ((slot, tier_index) for _, (slot, tier_index) in candidats
             if remaining.get(slot.key(), 0) >= p.count),
            None,
        )
        if chosen:
            slot, tier_index = chosen
            remaining[slot.key()] -= p.count
            assignments.append(Assignment(p.id, p.name, slot, p.count, tier_index))
        else:
            assignments.append(Assignment(p.id, p.name, None, p.count, None))
    return assignments


def run_final_draw(participants: list[Participant], supply: list[SlotSupply], seed: int) -> DrawResult:
    """Exécute un tirage avec la graine donnée. Peut être relancé
    (cf. `run_draw_iteration` plus bas pour la version tracée/auditable,
    utilisée par l'appli) — ce n'est QUE la publication finale
    (`Repo.mark_published`) qui est, elle, irréversible."""
    actifs = [p for p in participants if p.count > 0]
    order = weighted_draw_order(actifs, seed)
    assignments = allocate_by_draw_order(actifs, supply, order)
    return DrawResult(seed=seed, draw_order=order, assignments=assignments)


def preview_probabilities(
    participants: list[Participant], supply: list[SlotSupply], n_simulations: int = 500,
) -> dict[str, dict[str, float]]:
    """Aperçu PENDANT la collecte (pas le tirage final) : simule le tirage
    `n_simulations` fois avec des graines ALÉATOIRES à chaque fois (jamais
    la graine finale), et renvoie, pour chaque participant, la probabilité
    empirique d'obtenir chacun des créneaux qu'il a demandés — 100% si le
    créneau n'est pas en tension, moins sinon. Ne modifie rien, ne
    s'enregistre nulle part : un aperçu recalculé à chaque appel."""
    actifs = [p for p in participants if p.count > 0]
    comptes: dict[str, dict[str, int]] = {p.id: {} for p in actifs}
    for _ in range(n_simulations):
        seed = random.SystemRandom().randrange(2**32)
        result = run_final_draw(actifs, supply, seed)
        for a in result.assignments:
            if a.slot is not None:
                comptes[a.participant_id][a.slot.key()] = comptes[a.participant_id].get(a.slot.key(), 0) + 1
    return {
        pid: {slot_key: n / n_simulations for slot_key, n in slot_counts.items()}
        for pid, slot_counts in comptes.items()
    }


# --------------------------------------------------------------------------
# Relances tracées : le gestionnaire peut relancer le tirage, mais chaque
# essai est enregistré, visible de tous, et justifié — seule la
# PUBLICATION (Repo.mark_published) reste, elle, irréversible.
# --------------------------------------------------------------------------

@dataclass
class DrawRecord:
    show_id: str
    iteration: int
    seed: int
    justification: str
    created_by: str
    created_at: datetime
    draw_order: list[str]
    assignments: list[Assignment]


def _ensure_draw_tables(repo: Repo) -> None:
    repo.conn.execute("""
        CREATE TABLE IF NOT EXISTS repartition_draw_base (
            show_id TEXT PRIMARY KEY, base_seed INTEGER NOT NULL
        )
    """)
    repo.conn.execute("""
        CREATE TABLE IF NOT EXISTS repartition_draws (
            show_id TEXT NOT NULL, iteration INTEGER NOT NULL, seed INTEGER NOT NULL,
            justification TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            draw_order TEXT NOT NULL, assignments TEXT NOT NULL,
            PRIMARY KEY (show_id, iteration)
        )
    """)
    repo.conn.commit()


def _base_seed(repo: Repo, show_id: str) -> int:
    """Généré une seule fois par édition, puis réutilisé pour dériver la
    graine de chaque itération — ce n'est PAS un nouveau tirage au hasard
    à chaque relance, seule la partie qui encode le numéro d'itération
    change, de façon visible et vérifiable par tous."""
    _ensure_draw_tables(repo)
    row = repo.conn.execute(
        "SELECT base_seed FROM repartition_draw_base WHERE show_id=?", (show_id,)
    ).fetchone()
    if row:
        return row[0]
    base = secrets.randbelow(10**9)
    repo.conn.execute("INSERT INTO repartition_draw_base VALUES (?,?)", (show_id, base))
    repo.conn.commit()
    return base


def _derive_seed(base_seed: int, iteration: int) -> int:
    """Même base, seuls les 4 derniers chiffres changent (le numéro
    d'itération) : n'importe qui peut vérifier, en lisant la graine, que
    les tirages successifs d'une même édition sont bien dérivés les uns
    des autres et pas choisis au hasard parmi des graines sans lien."""
    return base_seed * 10_000 + iteration


def list_draws(repo: Repo, show_id: str) -> list[DrawRecord]:
    """Visible par TOUT LE MONDE (aucune restriction de rôle côté appli) —
    la transparence est le principe même de cette fonctionnalité."""
    _ensure_draw_tables(repo)
    rows = repo.conn.execute(
        "SELECT * FROM repartition_draws WHERE show_id=? ORDER BY iteration", (show_id,)
    ).fetchall()
    records = []
    for show_id_, iteration, seed, justification, created_by, created_at, draw_order_json, assignments_json in rows:
        assignments = [
            Assignment(d["participant_id"], d["participant_name"],
                       Slot.from_key(d["slot_key"]) if d["slot_key"] else None,
                       d["count"], d["tier_index"])
            for d in json.loads(assignments_json)
        ]
        records.append(DrawRecord(
            show_id=show_id_, iteration=iteration, seed=seed, justification=justification,
            created_by=created_by, created_at=datetime.fromisoformat(created_at),
            draw_order=json.loads(draw_order_json), assignments=assignments,
        ))
    return records


def run_draw_iteration(
    repo: Repo, show_id: str, participants: list[Participant], supply: list[SlotSupply],
    justification: str, created_by: str,
) -> DrawRecord:
    """Un tirage CANDIDAT, pas encore publié : enregistré intégralement
    (graine, ordre, résultat, qui, quand, pourquoi) pour que n'importe qui
    puisse ensuite voir combien de fois ça a été relancé et avec quelle
    justification, même si cette itération n'est finalement pas celle
    retenue."""
    if repo.is_published(show_id):
        raise RuntimeError("Cette édition est déjà publiée — plus aucun tirage n'est possible.")
    if not justification or not justification.strip():
        raise ValueError("Une justification est requise pour lancer (ou relancer) un tirage.")

    _ensure_draw_tables(repo)
    base = _base_seed(repo, show_id)
    iteration = (repo.conn.execute(
        "SELECT MAX(iteration) FROM repartition_draws WHERE show_id=?", (show_id,)
    ).fetchone()[0] or 0) + 1
    seed = _derive_seed(base, iteration)

    resultat = run_final_draw(participants, supply, seed)
    assignments_json = json.dumps([
        {"participant_id": a.participant_id, "participant_name": a.participant_name,
         "slot_key": a.slot.key() if a.slot else None, "count": a.count, "tier_index": a.tier_index}
        for a in resultat.assignments
    ])
    repo.conn.execute(
        "INSERT INTO repartition_draws VALUES (?,?,?,?,?,?,?,?)",
        (show_id, iteration, seed, justification.strip(), created_by, datetime.now().isoformat(),
         json.dumps(resultat.draw_order), assignments_json),
    )
    repo.conn.commit()
    return DrawRecord(
        show_id=show_id, iteration=iteration, seed=seed, justification=justification.strip(),
        created_by=created_by, created_at=datetime.now(), draw_order=resultat.draw_order,
        assignments=resultat.assignments,
    )
