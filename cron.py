"""Job quotidien d'échange-billet — à lancer à midi via systemd timer,
sur le modèle de probastack.py.

Trois choses à chaque exécution :
1. Calcule le cycle de matching du jour (expire les propositions de la
   veille non traitées, recalcule, crée les nouvelles propositions) —
   dons, bilatéral (longueur 2) ET cycles à N parties si le gestionnaire
   a relevé la longueur maximale (`MAX_CYCLE_LENGTH_CEILING` côté
   déploiement, puis `Repo.get_max_cycle_length` pour la valeur
   effective de cette édition) : UN SEUL moteur pour les trois.
2. Notifie chaque participant de chaque nouvelle proposition — avec un
   avertissement spécifique si elle implique plus de 2 personnes.
3. Valide automatiquement les transactions où une partie a confirmé et
   l'autre est restée silencieuse plus de AUTO_VALIDATE_DAYS jours, et
   notifie les deux parties (la partie auto-validée peut encore
   contester depuis l'appli pendant la fenêtre de grâce).
"""
import os
from datetime import timedelta

import yaml

from models import Repo, Slot, run_daily_matching_cycle
import notif

SHOW_ID = os.environ.get("SHOW_ID", "noel-2026")
DB_PATH = os.environ.get("ECHANGE_BILLET_DB", "echange-billet.db")
CYCLE_VALIDITY = timedelta(hours=24)
AUTO_VALIDATE_DELAY = timedelta(days=int(os.environ.get("AUTO_VALIDATE_DAYS", "1")))


def _load_max_cycle_length_ceiling() -> int:
    """Même source que app.py (CONFIG_DIR/config.yml, clé
    `max_cycle_length_ceiling`) — ce plafond de déploiement ne doit
    jamais diverger entre le cron et l'appli web."""
    config_dir = os.environ.get("CONFIG_DIR")
    if not config_dir:
        return 2
    path = os.path.join(config_dir, "config.yml")
    if not os.path.exists(path):
        return 2
    with open(path) as f:
        config = yaml.safe_load(f) or {}
    return int(config.get("max_cycle_length_ceiling", 2))


MAX_CYCLE_LENGTH_CEILING = _load_max_cycle_length_ceiling()


def run() -> None:
    repo = Repo(DB_PATH)

    max_len = repo.get_max_cycle_length(SHOW_ID, MAX_CYCLE_LENGTH_CEILING)
    _expired, created = run_daily_matching_cycle(repo, SHOW_ID, CYCLE_VALIDITY, max_cycle_len=max_len)

    for p in created:
        n = len(p.participant_ids)
        avertissement = (
            f" ATTENTION : vous faites partie d'une proposition à {n} personnes — si vous "
            f"acceptez, {n - 1} autre(s) personne(s) compte(nt) sur vous pour la suite de la chaîne."
            if n > 2 else ""
        )
        base = (
            "Rendez-vous sur échange-billet pour confirmer votre engagement — cette "
            "proposition expire demain à midi si vous ne répondez pas."
        )
        for giver_id, receiver_id, slot_key, count, _ in p.steps:
            giver, receiver = repo.get_listing(giver_id), repo.get_listing(receiver_id)
            slot = Slot.from_key(slot_key)
            notif.notify(
                [giver.user_email],
                f"Quelqu'un est intéressé par votre créneau {slot} ({count} place(s)).{avertissement} {base}",
            )
            notif.notify(
                [receiver.user_email],
                f"Une correspondance a été trouvée pour votre demande : {count} place(s) — "
                f"{slot}.{avertissement} {base}",
            )
    # Note : les propositions expirées ne sont pas notifiées individuellement
    # pour éviter le bruit ; chacun voit l'état réel ("en attente") directement
    # dans l'appli à chaque visite.

    validated = repo.run_auto_validation(SHOW_ID, AUTO_VALIDATE_DELAY)
    for t in validated:
        la, lb = repo.get_listing(t.listing_a_id), repo.get_listing(t.listing_b_id)
        notif.notify(
            [la.user_email, lb.user_email],
            f"L'échange de {t.count} place(s) pour {t.slot} a été validé automatiquement "
            f"(pas de retour de l'une des parties). Vous pouvez encore le contester depuis "
            f"l'appli si besoin.",
        )


if __name__ == "__main__":
    run()
