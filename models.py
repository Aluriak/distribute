"""Modèle de données et matching pour échange-billet.

v6 — UNIFICATION : il n'existe plus deux mécanismes séparés (un pour le
bilatéral, un pour les cycles à N parties). Un échange à 2 personnes
n'est qu'un cycle de longueur 2 ; un don est une relation à une seule
étape (un don, un receveur, pas de retour). Les TROIS cas (don, bilatéral,
cycle à N) partagent désormais :

- UNE seule `Proposition` générique : une liste d'étapes (qui donne quoi
  à qui), et un engagement individuel requis de chaque participant
  (`accepted`). Le refus d'un seul défait toute la proposition.
- UN seul registre de réservation (`_reserved_offered` / `_reserved_group`),
  qui lit indifféremment les Proposition en attente et les Transaction
  actives — plus de risque qu'un mécanisme ignore ce que l'autre a déjà
  réservé (c'était le cas avant l'unification : une CycleProposition en
  attente était invisible du moteur bilatéral).
- UNE fois tous les participants engagés, chaque étape devient une
  `Transaction` ORDINAIRE (une par relation donneur → receveur, même pour
  le bilatéral — qui en produit donc deux, pas une avec un champ
  "réciproque"), qui repasse par le même cycle confirm / dispute /
  validation automatique / litige / contestation, quelle que soit la
  longueur du cycle d'origine.

Une demande (`wanted`) reste une liste de GROUPES indépendants, chacun
avec son propre effectif et son propre jeu de créneaux acceptés (ou
aucun, pour "n'importe quel créneau valide") — cf. v5. Chaque groupe est
matché indépendamment, potentiellement avec des donneurs différents et à
des moments différents.
"""
from __future__ import annotations

import sqlite3
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from enum import Enum
from typing import Optional


class ListingType(str, Enum):
    RECHERCHE = "recherche"
    ECHANGE = "echange"   # wanted_groups vide => don pur


class ListingStatus(str, Enum):
    OUVERTE = "ouverte"
    CLOTUREE = "cloturee"


class SlotStatus(str, Enum):
    EN_ATTENTE = "en_attente"
    VALIDE = "valide"
    REJETE = "rejete"


class PropositionStatus(str, Enum):
    EN_ATTENTE = "en_attente"
    ACCEPTEE = "acceptee"      # tous ont accepté -> Transaction(s) créée(s)
    REFUSEE = "refusee"         # un seul refus suffit à défaire toute la proposition
    EXPIREE = "expiree"
    ECHOUEE = "echouee"         # tous ont accepté mais les capacités avaient changé entre-temps (course, annonce close...)


class TransactionStatus(str, Enum):
    PROPOSEE = "proposee"
    REALISEE = "realisee"
    ANNULEE = "annulee"
    LITIGE = "litige"


@dataclass(frozen=True)
class Slot:
    jour: date
    heure: time

    def key(self) -> str:
        return f"{self.jour.isoformat()}T{self.heure.isoformat(timespec='minutes')}"

    @classmethod
    def from_key(cls, key: str) -> "Slot":
        d, h = key.split("T")
        return cls(date.fromisoformat(d), time.fromisoformat(h))

    def __str__(self) -> str:
        return f"{self.jour.strftime('%A %d %B')} {self.heure.strftime('%Hh%M')}"


@dataclass
class WantedGroup:
    id: str
    count: int
    slots: list[Slot]   # ordre de préférence ; liste vide = n'importe quel créneau validé

    def __str__(self) -> str:
        if not self.slots:
            return f"{self.count} place(s) (n'importe quel créneau)"
        return f"{self.count} place(s) parmi : " + ", ".join(str(s) for s in self.slots)


def new_wanted_group(count: int, slots: Optional[list[Slot]] = None) -> WantedGroup:
    return WantedGroup(id=str(uuid.uuid4()), count=count, slots=slots or [])


@dataclass
class Show:
    id: str
    label: str


@dataclass
class Listing:
    id: str
    show_id: str
    type: ListingType
    user_email: str
    user_name: str
    offered_slot: Optional[Slot]
    offered_count: int
    wanted_groups: list[WantedGroup]
    is_gift: bool = False    # True seulement pour un don explicite ; distinct de "pas encore décidé"
    status: ListingStatus = ListingStatus.OUVERTE
    created_at: datetime = field(default_factory=datetime.now)
    note: str = ""

    def wanted_total(self) -> int:
        return sum(g.count for g in self.wanted_groups)

    def group(self, group_id: str) -> Optional[WantedGroup]:
        return next((g for g in self.wanted_groups if g.id == group_id), None)


def listing_label(l: Listing) -> str:
    if l.type == ListingType.RECHERCHE:
        return "recherche"
    if l.is_gift:
        return "don"
    return "depot" if not l.wanted_groups else "echange"


def group_accepts(group: WantedGroup, slot: Slot) -> bool:
    return not group.slots or slot in group.slots


# --------------------------------------------------------------------------
# Proposition — générique : une ou plusieurs étapes (don = 1, bilatéral = 2,
# cycle à N = N), chaque participant doit accepter individuellement.
# --------------------------------------------------------------------------

# Une étape : (giver_listing_id, receiver_listing_id, slot_key, count, seeker_group_id)
Step = tuple[str, str, str, int, str]


@dataclass
class Proposition:
    """Généralise un don (une seule étape : un donneur, un receveur, pas de
    retour), un échange bilatéral (deux étapes formant une boucle fermée :
    A donne à B, B donne à A), et un cycle à N parties (N étapes). Même
    mécanisme dans tous les cas : chaque participant doit cliquer
    individuellement « je m'engage » ; le refus d'un seul défait toute la
    proposition et libère les capacités. Une fois tous engagés, chaque
    étape devient une Transaction séparée."""
    id: str
    show_id: str
    steps: list[Step]
    status: PropositionStatus = PropositionStatus.EN_ATTENTE
    created_at: datetime = field(default_factory=datetime.now)
    accepted: dict[str, Optional[str]] = field(default_factory=dict)  # listing_id -> iso timestamp ou None
    decided_at: Optional[datetime] = None

    @property
    def participant_ids(self) -> list[str]:
        """L'ensemble des annonces impliquées — donneur ou receveur d'au
        moins une étape. Pour un don (1 étape) : les 2 (donneur, receveur).
        Pour un bilatéral (2 étapes en boucle) : les 2 mêmes annonces,
        chacune donneuse d'une étape et receveuse de l'autre. Pour un
        cycle à N : les N annonces du cycle."""
        ids: list[str] = []
        for giver_id, receiver_id, _, _, _ in self.steps:
            if giver_id not in ids:
                ids.append(giver_id)
            if receiver_id not in ids:
                ids.append(receiver_id)
        return ids

    @property
    def accepted_by_all(self) -> bool:
        return all(self.accepted.get(pid) is not None for pid in self.participant_ids)


@dataclass
class Transaction:
    """Une seule relation donneur -> receveur. Un échange bilatéral en
    produit DEUX (une par sens), un don UNE, un cycle à N en produit N —
    chacune suit indépendamment le même cycle confirm / dispute /
    validation automatique / litige / contestation."""
    id: str
    show_id: str
    listing_a_id: str   # receveur
    seeker_group_id: str
    listing_b_id: str   # donneur
    slot: Slot
    count: int
    status: TransactionStatus = TransactionStatus.PROPOSEE
    proposed_at: datetime = field(default_factory=datetime.now)
    confirmed_a_at: Optional[datetime] = None
    confirmed_b_at: Optional[datetime] = None
    disputed_a_at: Optional[datetime] = None
    disputed_b_at: Optional[datetime] = None
    auto_side: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None

    @property
    def confirmed_both(self) -> bool:
        return self.confirmed_a_at is not None and self.confirmed_b_at is not None


@dataclass
class CycleStep:
    """Intermédiaire typé (objets Listing/Slot complets, pas juste des
    ids) produit par `find_exchange_cycles` — pratique pour composer les
    notifications avant de persister en Proposition.steps."""
    giver: Listing
    receiver: Listing
    slot: Slot
    count: int
    group_id: str


_PROP_COLUMNS = ("id", "show_id", "steps", "status", "created_at", "accepted", "decided_at")
_TXN_COLUMNS = (
    "id", "show_id", "listing_a_id", "seeker_group_id", "listing_b_id", "slot_key", "count", "status",
    "proposed_at", "confirmed_a_at", "confirmed_b_at", "disputed_a_at", "disputed_b_at",
    "auto_side", "resolved_by", "resolved_at",
)


class Repo:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        # WAL : le cron quotidien (cron.py) et l'appli web écrivent toutes
        # deux dans ce même fichier ; le mode par défaut de SQLite peut
        # renvoyer "database is locked" sous écriture concurrente, WAL le
        # rend bien plus tolérant à ça.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                id TEXT PRIMARY KEY, show_id TEXT NOT NULL, type TEXT NOT NULL,
                user_email TEXT NOT NULL, user_name TEXT NOT NULL,
                offered_slot TEXT, offered_count INTEGER NOT NULL DEFAULT 0,
                wanted_groups TEXT NOT NULL, is_gift INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                created_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS slots (
                show_id TEXT NOT NULL, slot_key TEXT NOT NULL, status TEXT NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, capacity INTEGER,
                PRIMARY KEY (show_id, slot_key)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS repartition_preferences (
                show_id TEXT NOT NULL, user_email TEXT NOT NULL, user_name TEXT NOT NULL,
                count INTEGER NOT NULL, tiers TEXT NOT NULL, submitted_at TEXT NOT NULL,
                PRIMARY KEY (show_id, user_email)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS repartition_publications (
                show_id TEXT PRIMARY KEY, published_at TEXT NOT NULL, published_by TEXT NOT NULL,
                seed INTEGER, draw_order TEXT, iteration INTEGER
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS repartition_historique (
                user_email TEXT NOT NULL, show_id TEXT NOT NULL, tier_obtenu INTEGER,
                recorded_at TEXT NOT NULL, PRIMARY KEY (user_email, show_id)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS cycle_settings (
                show_id TEXT PRIMARY KEY, max_cycle_length INTEGER NOT NULL
            )
        """)
        self.conn.execute(f"""
            CREATE TABLE IF NOT EXISTS propositions (
                {", ".join(c + (" TEXT PRIMARY KEY" if c == "id" else " TEXT") for c in _PROP_COLUMNS)}
            )
        """)
        self.conn.execute(f"""
            CREATE TABLE IF NOT EXISTS transactions (
                {", ".join(c + (" TEXT PRIMARY KEY" if c == "id" else " INTEGER" if c == "count" else " TEXT") for c in _TXN_COLUMNS)}
            )
        """)
        self.conn.commit()

    # -- listings ------------------------------------------------------------------

    def add_listing(self, listing: Listing) -> None:
        self.conn.execute(
            "INSERT INTO listings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (listing.id, listing.show_id, listing.type.value, listing.user_email, listing.user_name,
             listing.offered_slot.key() if listing.offered_slot else None, listing.offered_count,
             self._groups_to_json(listing.wanted_groups), int(listing.is_gift),
             listing.status.value, listing.created_at.isoformat(), listing.note),
        )
        self.conn.commit()

    @staticmethod
    def _groups_to_json(groups: list[WantedGroup]) -> str:
        return json.dumps([{"id": g.id, "count": g.count, "slots": [s.key() for s in g.slots]} for g in groups])

    def update_wanted_groups(self, listing_id: str, wanted_groups: list[WantedGroup]) -> None:
        """Utilisé par propose_exchange : enrichit les critères d'acceptation
        d'une annonce existante (la sienne, ou — si elle était en dépôt —
        celle de la cible) sans toucher au reste de l'annonce."""
        self.conn.execute(
            "UPDATE listings SET wanted_groups=? WHERE id=?",
            (self._groups_to_json(wanted_groups), listing_id),
        )
        self.conn.commit()

    def get_listing(self, listing_id: str) -> Listing:
        row = self.conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        return self._row_to_listing(row)

    def open_listings(self, show_id: str) -> list[Listing]:
        rows = self.conn.execute(
            "SELECT * FROM listings WHERE show_id=? AND status != ? ORDER BY created_at",
            (show_id, ListingStatus.CLOTUREE.value),
        ).fetchall()
        return [self._row_to_listing(r) for r in rows]

    def close_listing(self, listing_id: str) -> None:
        self.conn.execute("UPDATE listings SET status=? WHERE id=?", (ListingStatus.CLOTUREE.value, listing_id))
        self.conn.commit()

    @staticmethod
    def _row_to_listing(row) -> Listing:
        (id_, show_id, type_, email, name, offered, offered_count, groups_json, is_gift,
         status, created_at, note) = row
        groups = [
            WantedGroup(id=g["id"], count=g["count"], slots=[Slot.from_key(k) for k in g["slots"]])
            for g in json.loads(groups_json)
        ]
        return Listing(
            id=id_, show_id=show_id, type=ListingType(type_), user_email=email, user_name=name,
            offered_slot=Slot.from_key(offered) if offered else None, offered_count=offered_count,
            wanted_groups=groups, is_gift=bool(is_gift), status=ListingStatus(status),
            created_at=datetime.fromisoformat(created_at), note=note,
        )

    # -- propose : se déclarer intéressé par l'annonce d'un tiers -------------------

    def remaining_offered(self, show_id: str, listing: Listing) -> int:
        active_txns = self.active_transactions(show_id)
        active_props = self.active_propositions(show_id)
        return listing.offered_count - _reserved_offered(active_txns, active_props, listing.id)

    def remaining_group(self, show_id: str, group: WantedGroup) -> int:
        active_txns = self.active_transactions(show_id)
        active_props = self.active_propositions(show_id)
        return group.count - _reserved_group(active_txns, active_props, group.id)

    def eligible_listings_for(self, show_id: str, user_email: str, target: Listing) -> list[Listing]:
        """Mes propres annonces d'échange qui pourraient se proposer pour
        `target` : j'ai des places à offrir, et soit `target` est en dépôt
        (aucun critère, donc n'importe quel ticket convient), soit mon
        créneau offert correspond à l'un de ses groupes avec de la place."""
        out = []
        for l in self.open_listings(show_id):
            if l.user_email != user_email or not l.offered_slot or l.is_gift:
                continue
            if self.remaining_offered(show_id, l) <= 0:
                continue
            if not target.wanted_groups:
                out.append(l)
            elif any(
                group_accepts(g, l.offered_slot) and self.remaining_group(show_id, g) > 0
                for g in target.wanted_groups
            ):
                out.append(l)
        return out

    def propose_exchange(self, show_id: str, my_listing_id: str, target_listing_id: str) -> tuple[bool, str]:
        """Enregistre mon critère d'acceptation (« j'échangerais mon ticket
        contre le sien ») — et, si la cible était en dépôt, la réciproque
        pour elle aussi. Ne crée PAS de Transaction ni de Proposition
        immédiatement : le prochain cycle quotidien s'en servira pour
        chercher le meilleur appariement global, pas nécessairement avec
        cette personne précise."""
        my = self.get_listing(my_listing_id)
        target = self.get_listing(target_listing_id)
        if my.user_email == target.user_email:
            return False, "Vous ne pouvez pas vous proposer à vous-même."
        if not my.offered_slot or my.is_gift:
            return False, "Vous devez avoir une annonce d'échange avec des places à offrir."
        if not target.offered_slot or target.is_gift:
            return False, "Cette offre ne nécessite pas de proposition."

        my_remaining = self.remaining_offered(show_id, my)
        target_remaining = self.remaining_offered(show_id, target)
        if my_remaining <= 0 or target_remaining <= 0:
            return False, "Plus de places disponibles des deux côtés."

        # protection contre le double-clic / la double soumission : si j'ai
        # déjà un groupe acceptant précisément ce créneau, inutile d'en
        # recréer un second (qui gonflerait artificiellement ma demande)
        if any(g.slots == [target.offered_slot] for g in my.wanted_groups):
            return False, "Vous vous êtes déjà proposé pour cette offre."

        n = min(my_remaining, target_remaining)
        target_group = next(
            (g for g in target.wanted_groups if group_accepts(g, my.offered_slot) and self.remaining_group(show_id, g) > 0),
            None,
        )
        if target_group is not None:
            n = min(n, self.remaining_group(show_id, target_group))
        elif target.wanted_groups:
            return False, "Vous n'avez pas le bon prérequis pour cette offre."
        # sinon : target.wanted_groups est vide (dépôt) -> toujours éligible

        self.update_wanted_groups(my.id, my.wanted_groups + [new_wanted_group(n, [target.offered_slot])])
        if target_group is None:
            self.update_wanted_groups(target.id, target.wanted_groups + [new_wanted_group(n, [my.offered_slot])])

        return True, "Votre critère d'acceptation a été enregistré pour le prochain calcul (midi)."

    # -- slots ---------------------------------------------------------------------

    def get_or_create_slot(self, show_id: str, slot: Slot, created_by: str) -> SlotStatus:
        row = self.conn.execute(
            "SELECT status FROM slots WHERE show_id=? AND slot_key=?", (show_id, slot.key())
        ).fetchone()
        if row:
            return SlotStatus(row[0])
        self.conn.execute(
            "INSERT INTO slots VALUES (?,?,?,?,?,?)",
            (show_id, slot.key(), SlotStatus.EN_ATTENTE.value, created_by, datetime.now().isoformat(), None),
        )
        self.conn.commit()
        return SlotStatus.EN_ATTENTE

    def set_slot_status(self, show_id: str, slot: Slot, status: SlotStatus) -> None:
        self.conn.execute(
            "UPDATE slots SET status=? WHERE show_id=? AND slot_key=?",
            (status.value, show_id, slot.key()),
        )
        self.conn.commit()

    def set_slot_capacity(self, show_id: str, slot: Slot, capacity: int, defined_by: str) -> None:
        """Réservé au rôle gestionnaire : définit (ou crée) un créneau avec
        son nombre de billets disponibles. Un créneau ainsi défini est
        d'emblée VALIDE — le gestionnaire est autoritaire, pas besoin de la
        file de modération prévue pour les créneaux tapés librement par les
        utilisateurs dans leurs annonces."""
        existing = self.conn.execute(
            "SELECT 1 FROM slots WHERE show_id=? AND slot_key=?", (show_id, slot.key())
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE slots SET capacity=?, status=? WHERE show_id=? AND slot_key=?",
                (capacity, SlotStatus.VALIDE.value, show_id, slot.key()),
            )
        else:
            self.conn.execute(
                "INSERT INTO slots VALUES (?,?,?,?,?,?)",
                (show_id, slot.key(), SlotStatus.VALIDE.value, defined_by, datetime.now().isoformat(), capacity),
            )
        self.conn.commit()

    def list_slots(self, show_id: str, status: Optional[SlotStatus] = None) -> list[tuple[Slot, SlotStatus, Optional[int]]]:
        if status:
            rows = self.conn.execute(
                "SELECT slot_key, status, capacity FROM slots WHERE show_id=? AND status=?", (show_id, status.value)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT slot_key, status, capacity FROM slots WHERE show_id=?", (show_id,)
            ).fetchall()
        return [(Slot.from_key(k), SlotStatus(s), cap) for k, s, cap in rows]

    # -- longueur maximale de cycle (gestionnaire) -----------------------------------

    def get_max_cycle_length(self, show_id: str, ceiling: int) -> int:
        """Longueur effective pour cette édition : la valeur choisie par le
        gestionnaire si elle existe (toujours bornée au plafond de
        déploiement, au cas où celui-ci aurait baissé depuis), sinon 2
        (cycles à N parties désactivés, seul le bilatéral — longueur 2 —
        tourne)."""
        row = self.conn.execute(
            "SELECT max_cycle_length FROM cycle_settings WHERE show_id=?", (show_id,)
        ).fetchone()
        if not row:
            return 2
        return min(row[0], ceiling) if ceiling >= 3 else 2

    def set_max_cycle_length(self, show_id: str, value: int, ceiling: int) -> tuple[bool, str]:
        if ceiling < 3:
            return False, "Les cycles à N parties sont désactivés au déploiement (plafond < 3)."
        if not (3 <= value <= ceiling):
            return False, f"La valeur doit être comprise entre 3 et {ceiling}."
        self.conn.execute(
            "INSERT INTO cycle_settings VALUES (?,?) ON CONFLICT(show_id) DO UPDATE SET max_cycle_length=excluded.max_cycle_length",
            (show_id, value),
        )
        self.conn.commit()
        return True, "Longueur maximale de cycle mise à jour."

    # -- préférences de répartition (collecte en amont, pour le solveur) ------------

    def submit_preference(self, show_id: str, user_email: str, user_name: str,
                           count: int, tiers: list[list[Slot]]) -> None:
        """Remplace la préférence existante de cet utilisateur pour ce
        spectacle, le cas échéant (un utilisateur ne peut avoir qu'une
        préférence active à la fois — la dernière soumission fait foi)."""
        tiers_json = json.dumps([[s.key() for s in tier] for tier in tiers])
        self.conn.execute(
            "INSERT INTO repartition_preferences VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(show_id, user_email) DO UPDATE SET "
            "user_name=excluded.user_name, count=excluded.count, tiers=excluded.tiers, "
            "submitted_at=excluded.submitted_at",
            (show_id, user_email, user_name, count, tiers_json, datetime.now().isoformat()),
        )
        self.conn.commit()

    def get_preference(self, show_id: str, user_email: str) -> Optional[tuple[int, list[list[Slot]], datetime]]:
        row = self.conn.execute(
            "SELECT count, tiers, submitted_at FROM repartition_preferences WHERE show_id=? AND user_email=?",
            (show_id, user_email),
        ).fetchone()
        if not row:
            return None
        count, tiers_json, submitted_at = row
        tiers = [[Slot.from_key(k) for k in tier] for tier in json.loads(tiers_json)]
        return count, tiers, datetime.fromisoformat(submitted_at)

    def list_preferences(self, show_id: str) -> list[tuple[str, str, int, list[list[Slot]]]]:
        """Retourne (email, nom, count, tiers) pour chaque préférence soumise."""
        rows = self.conn.execute(
            "SELECT user_email, user_name, count, tiers FROM repartition_preferences WHERE show_id=?",
            (show_id,),
        ).fetchall()
        return [
            (email, name, count, [[Slot.from_key(k) for k in tier] for tier in json.loads(tiers_json)])
            for email, name, count, tiers_json in rows
        ]

    # -- publication de la répartition (protection contre les doublons) ------------

    def is_published(self, show_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM repartition_publications WHERE show_id=?", (show_id,)
        ).fetchone() is not None

    def mark_published(
        self, show_id: str, published_by: str, seed: int, draw_order: list[str], iteration: int = 1,
    ) -> None:
        """`seed` et `draw_order` rendent le tirage auditable après coup :
        qui était dans le pool (draw_order), et avec quelle graine
        reproduire exactement le même résultat. `iteration` dit combien de
        fois le tirage a été relancé avant publication (iteration - 1) —
        cf. allocation.run_draw_iteration pour le détail tracé de chaque
        relance (graine dérivée, justification). Jamais republié une fois
        appelé — cf. is_published, vérifié avant tout nouveau tirage."""
        self.conn.execute(
            "INSERT INTO repartition_publications VALUES (?,?,?,?,?,?)",
            (show_id, datetime.now().isoformat(), published_by, seed, json.dumps(draw_order), iteration),
        )
        self.conn.commit()

    def get_publication(self, show_id: str) -> Optional[tuple[str, datetime, int, list[str], int]]:
        row = self.conn.execute(
            "SELECT published_by, published_at, seed, draw_order, iteration FROM repartition_publications "
            "WHERE show_id=?",
            (show_id,),
        ).fetchone()
        if not row:
            return None
        published_by, published_at, seed, draw_order_json, iteration = row
        return published_by, datetime.fromisoformat(published_at), seed, json.loads(draw_order_json), iteration or 1

    # -- historique inter-éditions (pour la pondération de priorité) --------------

    def record_historique(self, show_id: str, user_email: str, tier_obtenu: Optional[int]) -> None:
        """Enregistre le rang réellement obtenu par cette personne pour cette
        édition (None si non servie) — appelé automatiquement à la
        publication. Sert de mémoire aux éditions suivantes : voir
        allocation.compute_priority_weight."""
        self.conn.execute(
            "INSERT INTO repartition_historique VALUES (?,?,?,?) "
            "ON CONFLICT(user_email, show_id) DO UPDATE SET "
            "tier_obtenu=excluded.tier_obtenu, recorded_at=excluded.recorded_at",
            (user_email, show_id, tier_obtenu, datetime.now().isoformat()),
        )
        self.conn.commit()

    def get_historique(self, user_email: str, exclude_show_id: str) -> list[Optional[int]]:
        """Les rangs obtenus par cette personne lors des éditions
        PRÉCÉDENTES (toutes éditions confondues, hors celle en cours)."""
        rows = self.conn.execute(
            "SELECT tier_obtenu FROM repartition_historique WHERE user_email=? AND show_id != ?",
            (user_email, exclude_show_id),
        ).fetchall()
        return [r[0] for r in rows]

    # -- propositions (générique : don, bilatéral, cycle à N) -----------------------

    def add_proposition(self, p: Proposition) -> None:
        self.conn.execute(
            f"INSERT INTO propositions VALUES ({','.join('?' * len(_PROP_COLUMNS))})",
            (p.id, p.show_id, json.dumps(p.steps), p.status.value, p.created_at.isoformat(),
             json.dumps(p.accepted), p.decided_at.isoformat() if p.decided_at else None),
        )
        self.conn.commit()

    @staticmethod
    def _row_to_proposition(row) -> Proposition:
        d = dict(zip(_PROP_COLUMNS, row))
        return Proposition(
            id=d["id"], show_id=d["show_id"],
            steps=[tuple(s) for s in json.loads(d["steps"])],
            status=PropositionStatus(d["status"]),
            created_at=datetime.fromisoformat(d["created_at"]),
            accepted=json.loads(d["accepted"]),
            decided_at=datetime.fromisoformat(d["decided_at"]) if d["decided_at"] else None,
        )

    def get_proposition(self, proposition_id: str) -> Proposition:
        row = self.conn.execute("SELECT * FROM propositions WHERE id=?", (proposition_id,)).fetchone()
        return self._row_to_proposition(row)

    def active_propositions(self, show_id: str) -> list[Proposition]:
        rows = self.conn.execute(
            "SELECT * FROM propositions WHERE show_id=? AND status=?",
            (show_id, PropositionStatus.EN_ATTENTE.value),
        ).fetchall()
        return [self._row_to_proposition(r) for r in rows]

    def propositions_for_user(self, show_id: str, email: str) -> list[tuple[Proposition, str]]:
        """Toutes les propositions EN_ATTENTE où `email` est partie
        prenante, avec l'id de SON annonce impliquée. Que son propre côté
        ait déjà accepté ou non se lit sur `accepted[listing_id]` — à
        l'appelant de savoir s'il doit encore agir."""
        listings = {l.id: l for l in self.open_listings(show_id)}
        out = []
        for p in self.active_propositions(show_id):
            for pid in p.participant_ids:
                listing = listings.get(pid)
                if listing and listing.user_email == email:
                    out.append((p, pid))
        return out

    def expire_stale_propositions(self, show_id: str, older_than: timedelta) -> list[Proposition]:
        cutoff = (datetime.now() - older_than).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM propositions WHERE show_id=? AND status=? AND created_at < ?",
            (show_id, PropositionStatus.EN_ATTENTE.value, cutoff),
        ).fetchall()
        expired = [self._row_to_proposition(r) for r in rows]
        for p in expired:
            self.conn.execute(
                "UPDATE propositions SET status=?, decided_at=? WHERE id=?",
                (PropositionStatus.EXPIREE.value, datetime.now().isoformat(), p.id),
            )
        self.conn.commit()
        return expired

    def accept_proposition_step(self, proposition_id: str, listing_id: str) -> tuple[Optional[list[Transaction]], Proposition]:
        """Enregistre l'engagement de `listing_id`. Ne crée les Transaction
        (une par étape) que lorsque TOUS les participants ont accepté — et
        revalide alors les capacités (filet de sécurité pour les cas
        limites : une annonce close entre-temps, une course sous écriture
        concurrente) : si elles ne suffisent plus, la proposition échoue
        proprement (ECHOUEE) plutôt que de créer des échanges incohérents."""
        p = self.get_proposition(proposition_id)
        if p.status != PropositionStatus.EN_ATTENTE:
            return None, p
        if listing_id not in p.accepted:
            return None, p

        now = datetime.now().isoformat()
        p.accepted[listing_id] = now
        self.conn.execute("UPDATE propositions SET accepted=? WHERE id=?", (json.dumps(p.accepted), proposition_id))
        self.conn.commit()
        p = self.get_proposition(proposition_id)

        if not p.accepted_by_all:
            return None, p

        # Revalidation : exclure la réservation de CETTE proposition elle-même
        # (encore EN_ATTENTE à l'instant du calcul) — sinon elle se compte
        # comme "déjà prise par quelqu'un d'autre" et échoue systématiquement
        # sur elle-même.
        active_txns = self.active_transactions(p.show_id)
        active_props_autres = [ap for ap in self.active_propositions(p.show_id) if ap.id != proposition_id]
        for giver_id, receiver_id, slot_key, count, group_id in p.steps:
            giver = self.get_listing(giver_id)
            if giver.offered_count - _reserved_offered(active_txns, active_props_autres, giver_id) < count:
                self._fail_proposition(proposition_id)
                return None, self.get_proposition(proposition_id)
            groupe = next((g for g in self.get_listing(receiver_id).wanted_groups if g.id == group_id), None)
            if groupe is None or groupe.count - _reserved_group(active_txns, active_props_autres, group_id) < count:
                self._fail_proposition(proposition_id)
                return None, self.get_proposition(proposition_id)

        transactions = []
        for giver_id, receiver_id, slot_key, count, group_id in p.steps:
            t = Transaction(
                id=str(uuid.uuid4()), show_id=p.show_id,
                listing_a_id=receiver_id, seeker_group_id=group_id, listing_b_id=giver_id,
                slot=Slot.from_key(slot_key), count=count,
            )
            self.add_transaction(t)
            transactions.append(t)

        self.conn.execute(
            "UPDATE propositions SET status=?, decided_at=? WHERE id=?",
            (PropositionStatus.ACCEPTEE.value, datetime.now().isoformat(), proposition_id),
        )
        self.conn.commit()
        return transactions, self.get_proposition(proposition_id)

    def _fail_proposition(self, proposition_id: str) -> None:
        self.conn.execute(
            "UPDATE propositions SET status=?, decided_at=? WHERE id=?",
            (PropositionStatus.ECHOUEE.value, datetime.now().isoformat(), proposition_id),
        )
        self.conn.commit()

    def decline_proposition_step(
        self, proposition_id: str, listing_id: str, listing_no_longer_relevant: bool = False,
    ) -> None:
        """Un refus d'un seul participant défait toute la proposition —
        personne d'autre ne peut être forcé de donner sa place parce que
        le reste s'est engagé."""
        p = self.get_proposition(proposition_id)
        if p.status != PropositionStatus.EN_ATTENTE:
            return
        self.conn.execute(
            "UPDATE propositions SET status=?, decided_at=? WHERE id=? AND status=?",
            (PropositionStatus.REFUSEE.value, datetime.now().isoformat(), proposition_id,
             PropositionStatus.EN_ATTENTE.value),
        )
        self.conn.commit()
        if listing_no_longer_relevant:
            self.close_listing(listing_id)

    # -- transactions ----------------------------------------------------------------

    def add_transaction(self, t: Transaction) -> None:
        self.conn.execute(
            f"INSERT INTO transactions VALUES ({','.join('?' * len(_TXN_COLUMNS))})",
            self._transaction_to_row(t),
        )
        self.conn.commit()

    @staticmethod
    def _transaction_to_row(t: Transaction) -> tuple:
        return (
            t.id, t.show_id, t.listing_a_id, t.seeker_group_id, t.listing_b_id, t.slot.key(), t.count, t.status.value,
            t.proposed_at.isoformat(),
            t.confirmed_a_at.isoformat() if t.confirmed_a_at else None,
            t.confirmed_b_at.isoformat() if t.confirmed_b_at else None,
            t.disputed_a_at.isoformat() if t.disputed_a_at else None,
            t.disputed_b_at.isoformat() if t.disputed_b_at else None,
            t.auto_side, t.resolved_by, t.resolved_at.isoformat() if t.resolved_at else None,
        )

    @staticmethod
    def _row_to_transaction(row) -> Transaction:
        d = dict(zip(_TXN_COLUMNS, row))
        parse = lambda k: datetime.fromisoformat(d[k]) if d[k] else None
        return Transaction(
            id=d["id"], show_id=d["show_id"], listing_a_id=d["listing_a_id"],
            seeker_group_id=d["seeker_group_id"], listing_b_id=d["listing_b_id"],
            slot=Slot.from_key(d["slot_key"]), count=d["count"], status=TransactionStatus(d["status"]),
            proposed_at=parse("proposed_at"), confirmed_a_at=parse("confirmed_a_at"),
            confirmed_b_at=parse("confirmed_b_at"), disputed_a_at=parse("disputed_a_at"),
            disputed_b_at=parse("disputed_b_at"), auto_side=d["auto_side"],
            resolved_by=d["resolved_by"], resolved_at=parse("resolved_at"),
        )

    def _get_transaction(self, transaction_id: str) -> Transaction:
        row = self.conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
        return self._row_to_transaction(row)

    def active_transactions(self, show_id: str) -> list[Transaction]:
        """Transactions qui bloquent encore de la capacité : en attente
        (PROPOSEE), en litige, OU déjà réalisées. Seule ANNULEE libère la
        capacité — une fois RÉALISÉE, le ticket a réellement changé de
        main, sa capacité ne doit JAMAIS redevenir disponible, sans quoi le
        même ticket pourrait être repromis à quelqu'un d'autre."""
        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE show_id=? AND status IN (?,?,?)",
            (show_id, TransactionStatus.PROPOSEE.value, TransactionStatus.LITIGE.value,
             TransactionStatus.REALISEE.value),
        ).fetchall()
        return [self._row_to_transaction(r) for r in rows]

    def transactions_for_user(self, show_id: str, email: str) -> list[Transaction]:
        listings = {l.id: l for l in self.open_listings(show_id)}
        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE show_id=? AND status IN (?,?,?)",
            (show_id, TransactionStatus.PROPOSEE.value, TransactionStatus.LITIGE.value, TransactionStatus.REALISEE.value),
        ).fetchall()
        out = []
        for r in rows:
            t = self._row_to_transaction(r)
            la, lb = listings.get(t.listing_a_id), listings.get(t.listing_b_id)
            if (la and la.user_email == email) or (lb and lb.user_email == email):
                out.append(t)
        return out

    def litiges(self, show_id: str) -> list[Transaction]:
        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE show_id=? AND status=?", (show_id, TransactionStatus.LITIGE.value)
        ).fetchall()
        return [self._row_to_transaction(r) for r in rows]

    def confirm(self, transaction_id: str, side: str) -> Transaction:
        t = self._get_transaction(transaction_id)
        if t.status != TransactionStatus.PROPOSEE:
            return t
        now = datetime.now().isoformat()
        col = "confirmed_a_at" if side == "a" else "confirmed_b_at"
        self.conn.execute(f"UPDATE transactions SET {col}=? WHERE id=?", (now, transaction_id))
        t = self._get_transaction(transaction_id)
        if t.confirmed_a_at and t.confirmed_b_at:
            self.conn.execute(
                "UPDATE transactions SET status=? WHERE id=?", (TransactionStatus.REALISEE.value, transaction_id)
            )
        self.conn.commit()
        return self._get_transaction(transaction_id)

    def dispute(self, transaction_id: str, side: str) -> Transaction:
        t = self._get_transaction(transaction_id)
        if t.status != TransactionStatus.PROPOSEE:
            return t
        now = datetime.now().isoformat()
        col = "disputed_a_at" if side == "a" else "disputed_b_at"
        self.conn.execute(
            f"UPDATE transactions SET {col}=?, status=? WHERE id=?",
            (now, TransactionStatus.LITIGE.value, transaction_id),
        )
        self.conn.commit()
        return self._get_transaction(transaction_id)

    def contest(self, transaction_id: str, side: str, grace: timedelta) -> tuple[bool, Transaction]:
        t = self._get_transaction(transaction_id)
        if t.status != TransactionStatus.REALISEE or t.auto_side != side:
            return False, t
        auto_confirmed_at = t.confirmed_a_at if side == "a" else t.confirmed_b_at
        if auto_confirmed_at is None or datetime.now() - auto_confirmed_at > grace:
            return False, t
        self.conn.execute("UPDATE transactions SET status=? WHERE id=?", (TransactionStatus.LITIGE.value, transaction_id))
        self.conn.commit()
        return True, self._get_transaction(transaction_id)

    def resolve_litige(self, transaction_id: str, moderator_email: str, outcome: TransactionStatus) -> bool:
        """Retourne False sans rien changer si la transaction n'est plus en
        litige (déjà tranchée par quelqu'un d'autre, double-clic...) —
        empêche qu'une décision déjà prise soit silencieusement écrasée."""
        assert outcome in (TransactionStatus.REALISEE, TransactionStatus.ANNULEE)
        cur = self.conn.execute(
            "UPDATE transactions SET status=?, resolved_by=?, resolved_at=? WHERE id=? AND status=?",
            (outcome.value, moderator_email, datetime.now().isoformat(), transaction_id, TransactionStatus.LITIGE.value),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def run_auto_validation(self, show_id: str, delay: timedelta) -> list[Transaction]:
        cutoff = datetime.now() - delay
        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE show_id=? AND status=?",
            (show_id, TransactionStatus.PROPOSEE.value),
        ).fetchall()
        validated = []
        for r in rows:
            t = self._row_to_transaction(r)
            sides_confirmed = [t.confirmed_a_at is not None, t.confirmed_b_at is not None]
            if sum(sides_confirmed) != 1:
                continue
            confirming_side = "a" if t.confirmed_a_at else "b"
            silent_side = "b" if confirming_side == "a" else "a"
            confirmed_at = t.confirmed_a_at or t.confirmed_b_at
            if confirmed_at <= cutoff:
                col = "confirmed_a_at" if silent_side == "a" else "confirmed_b_at"
                now = datetime.now().isoformat()
                self.conn.execute(
                    f"UPDATE transactions SET {col}=?, status=?, auto_side=? WHERE id=?",
                    (now, TransactionStatus.REALISEE.value, silent_side, t.id),
                )
                validated.append(self._get_transaction(t.id))
        self.conn.commit()
        return validated


def new_listing(show_id, type_, user_email, user_name,
                 offered_slot=None, offered_count=0, wanted_groups=None, is_gift=False, note="") -> Listing:
    return Listing(
        id=str(uuid.uuid4()), show_id=show_id, type=ListingType(type_), user_email=user_email,
        user_name=user_name, offered_slot=offered_slot, offered_count=offered_count,
        wanted_groups=wanted_groups or [], is_gift=is_gift, note=note,
    )


# --------------------------------------------------------------------------
# Matching — UN seul registre de réservation, deux recherches (don /
# cycle fermé) qui alimentent toutes deux la même Proposition générique.
# --------------------------------------------------------------------------

def _reserved_offered(active_txns: list[Transaction], active_props: list[Proposition], listing_id: str) -> int:
    r = sum(t.count for t in active_txns if t.listing_b_id == listing_id)
    for p in active_props:
        r += sum(count for giver_id, _, _, count, _ in p.steps if giver_id == listing_id)
    return r


def _reserved_group(active_txns: list[Transaction], active_props: list[Proposition], group_id: str) -> int:
    r = sum(t.count for t in active_txns if t.seeker_group_id == group_id)
    for p in active_props:
        r += sum(count for _, _, _, count, gid in p.steps if gid == group_id)
    return r


def _match_gifts(
    show_id: str, listings: list[Listing], valid_slot_keys: set[str],
    remaining_offered: dict[str, int], remaining_group: dict[str, int],
) -> list[Proposition]:
    """Un don (is_gift=True) satisfait n'importe quel chercheur, sans
    réciprocité — une seule étape (le don lui-même), pas un cycle."""
    created: list[Proposition] = []
    work_items = [
        (seeker, group)
        for seeker in sorted(listings, key=lambda l: l.created_at)
        for group in seeker.wanted_groups
    ]
    for seeker, group in work_items:
        remaining = remaining_group[group.id]
        if remaining <= 0:
            continue
        acceptable = group.slots if group.slots else sorted(
            {l.offered_slot for l in listings if l.offered_slot}, key=lambda s: (s.jour, s.heure)
        )
        for slot in acceptable:
            if slot.key() not in valid_slot_keys or remaining <= 0:
                continue
            candidates = [
                l for l in listings
                if l.id != seeker.id and l.user_email != seeker.user_email
                and l.is_gift and l.offered_slot == slot and remaining_offered.get(l.id, 0) > 0
            ]
            candidates.sort(key=lambda l: l.created_at)
            for giver in candidates:
                if remaining <= 0:
                    break
                n = min(remaining, remaining_offered[giver.id])
                if n <= 0:
                    continue
                p = Proposition(
                    id=str(uuid.uuid4()), show_id=show_id,
                    steps=[(giver.id, seeker.id, slot.key(), n, group.id)],
                    accepted={giver.id: None, seeker.id: None},
                )
                created.append(p)
                remaining -= n
                remaining_group[group.id] = remaining
                remaining_offered[giver.id] -= n
    return created


def _find_pointer(
    listing: Listing, listings: list[Listing],
    remaining_offered: dict[str, int], remaining_group: dict[str, int],
    valid_slot_keys: set[str],
) -> Optional[tuple[Listing, Slot, str, WantedGroup]]:
    """Le 'pointeur' TTC de `listing` : le premier donneur compatible
    (encore disponible, non-don — un don n'entre jamais dans un cycle
    fermé, cf. _match_gifts) pour l'un de ses groupes non satisfaits, par
    ordre de préférence du groupe puis d'ancienneté du donneur."""
    for group in listing.wanted_groups:
        if remaining_group.get(group.id, 0) <= 0:
            continue
        candidate_slots = group.slots if group.slots else sorted(
            {l.offered_slot for l in listings if l.offered_slot and l.offered_slot.key() in valid_slot_keys},
            key=lambda s: (s.jour, s.heure),
        )
        for slot in candidate_slots:
            if slot.key() not in valid_slot_keys:
                continue
            candidates = [
                l for l in listings
                if l.id != listing.id and l.user_email != listing.user_email
                and not l.is_gift and l.offered_slot == slot and remaining_offered.get(l.id, 0) > 0
            ]
            candidates.sort(key=lambda l: l.created_at)
            if candidates:
                return candidates[0], slot, group.id, group
    return None


def find_exchange_cycles(
    listings: list[Listing], valid_slot_keys: set[str],
    remaining_offered: dict[str, int], remaining_group: dict[str, int],
    max_cycle_len: int = 2, max_rounds: int = 50,
) -> list[list[CycleStep]]:
    """Recherche de cycles d'échange fermés, de longueur 2 (bilatéral, cas
    le plus fréquent) jusqu'à `max_cycle_len` (Top Trading Cycles — cf.
    Shapley-Scarf). Un échange bilatéral N'EST QU'UN cycle de longueur 2 :
    même algorithme, aucun traitement séparé. Mute `remaining_offered`/
    `remaining_group` EN PLACE au fur et à mesure (pour composer avec
    `_match_gifts`, exécutée avant, sur le même registre)."""
    by_id = {l.id: l for l in listings}
    cycles: list[list[CycleStep]] = []

    for _ in range(max_rounds):
        pointer: dict[str, tuple[Listing, Slot, str, WantedGroup]] = {}
        for l in listings:
            found = _find_pointer(l, listings, remaining_offered, remaining_group, valid_slot_keys)
            if found:
                giver, slot, group_id, group = found
                pointer[l.id] = (giver, slot, group_id, group)

        if not pointer:
            break

        cycle_found_this_round = False
        visited: set[str] = set()
        for start_id in list(pointer.keys()):
            if start_id in visited:
                continue
            path: list[str] = []
            seen_in_path: dict[str, int] = {}
            current = start_id
            while current in pointer and current not in seen_in_path:
                seen_in_path[current] = len(path)
                path.append(current)
                current = pointer[current][0].id
            visited.update(path)

            if current in seen_in_path:
                start_idx = seen_in_path[current]
                cycle_ids = path[start_idx:]
                if 2 <= len(cycle_ids) <= max_cycle_len:
                    bottleneck = min(
                        min(remaining_offered[pointer[cid][0].id], remaining_group[pointer[cid][2]])
                        for cid in cycle_ids
                    )
                    if bottleneck <= 0:
                        continue
                    steps = []
                    for cid in cycle_ids:
                        giver, slot, group_id, _ = pointer[cid]
                        steps.append(CycleStep(
                            giver=giver, receiver=by_id[cid], slot=slot, count=bottleneck, group_id=group_id,
                        ))
                    for step in steps:
                        remaining_offered[step.giver.id] -= step.count
                        remaining_group[step.group_id] -= step.count
                    cycles.append(steps)
                    cycle_found_this_round = True

        if not cycle_found_this_round:
            break

    return cycles


def run_daily_matching_cycle(
    repo: Repo, show_id: str, cycle_validity: timedelta = timedelta(hours=24), max_cycle_len: int = 2,
) -> tuple[list[Proposition], list[Proposition]]:
    """LE moteur de matching, unique : dons (une étape, pas de réciprocité)
    PUIS cycles fermés de longueur 2 (bilatéral) à `max_cycle_len` (cycles
    à N parties, si le gestionnaire a relevé le plafond — cf.
    Repo.get_max_cycle_length). Même registre de réservation pour les
    deux recherches, donc aucun risque qu'elles se marchent dessus."""
    expired = repo.expire_stale_propositions(show_id, cycle_validity)

    listings = repo.open_listings(show_id)
    active_txns = repo.active_transactions(show_id)
    active_props = repo.active_propositions(show_id)
    valid_slots = [s for s, _, _ in repo.list_slots(show_id, SlotStatus.VALIDE)]
    valid_slot_keys = {s.key() for s in valid_slots}

    remaining_offered = {
        l.id: l.offered_count - _reserved_offered(active_txns, active_props, l.id)
        for l in listings if l.offered_slot
    }
    remaining_group = {
        g.id: g.count - _reserved_group(active_txns, active_props, g.id)
        for l in listings for g in l.wanted_groups
    }

    created: list[Proposition] = []

    created += _match_gifts(show_id, listings, valid_slot_keys, remaining_offered, remaining_group)

    for steps in find_exchange_cycles(listings, valid_slot_keys, remaining_offered, remaining_group, max_cycle_len):
        p = Proposition(
            id=str(uuid.uuid4()), show_id=show_id,
            steps=[(s.giver.id, s.receiver.id, s.slot.key(), s.count, s.group_id) for s in steps],
        )
        p.accepted = {pid: None for pid in p.participant_ids}
        created.append(p)

    for p in created:
        repo.add_proposition(p)

    return expired, created
