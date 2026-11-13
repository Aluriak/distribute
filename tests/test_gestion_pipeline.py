from datetime import date, time

from models import ListingType, SlotStatus, new_listing, Slot
from allocation import (
    Participant, SlotSupply, compute_priority_weight,
    run_final_draw, preview_probabilities,
)
from conftest import valid_slot


def test_capacite_auto_valide_le_creneau(repo, show_id):
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    repo.set_slot_capacity(show_id, s6, 50, "gest@chu.fr")

    assert repo.list_slots(show_id) == [(s6, SlotStatus.VALIDE, 50)]


def test_publication_persistee_bloque_republication(repo, show_id):
    assert repo.is_published(show_id) is False
    repo.mark_published(show_id, "gest@chu.fr", seed=1234, draw_order=["a@chu.fr"], iteration=3)
    assert repo.is_published(show_id) is True

    published_by, published_at, seed, draw_order, iteration = repo.get_publication(show_id)
    assert published_by == "gest@chu.fr" and seed == 1234 and draw_order == ["a@chu.fr"]
    assert iteration == 3  # donc 2 relances avant publication


def test_historique_influence_le_calcul_suivant(repo, show_id):
    repo.record_historique("noel-2024", "alice@chu.fr", 2)
    repo.record_historique("noel-2025", "alice@chu.fr", None)

    hist = repo.get_historique("alice@chu.fr", show_id)
    assert set(hist) == {2, None}
    assert compute_priority_weight(hist, unserved_penalty=3) > 1.0


def test_pipeline_complet_capacite_preference_tirage_publication(repo, show_id):
    s5 = valid_slot(repo, show_id, date(2026, 12, 5), time(15, 0))
    s6 = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    repo.set_slot_capacity(show_id, s5, 2, "gest@chu.fr")
    repo.set_slot_capacity(show_id, s6, 2, "gest@chu.fr")

    # les deux acceptent indifféremment s5 ou s6 : la capacité totale (4)
    # couvre la demande totale (4), donc les deux sont servis quel que soit
    # l'ordre tiré — contrairement à l'ancien solveur déterministe, un
    # tirage au sort ne garantit PAS qui obtient quoi, seulement que la
    # capacité suffisante empêche quiconque de rester sur le carreau
    repo.submit_preference(show_id, "alice@chu.fr", "Alice", 2, [[s5], [s6]])
    repo.submit_preference(show_id, "bob@chu.fr", "Bob", 2, [[s5], [s6]])
    repo.submit_preference(show_id, "denis@chu.fr", "Denis", 0, [])

    # aperçu probabiliste : ne doit rien persister, consultable pendant la collecte
    prefs = repo.list_preferences(show_id)
    participants = [Participant(e, n, c, t) for e, n, c, t in prefs if c > 0]
    supply = [SlotSupply(slot, cap) for slot, _, cap in repo.list_slots(show_id, SlotStatus.VALIDE) if cap]
    probas = preview_probabilities(participants, supply, n_simulations=100)
    assert set(probas.keys()) == {"alice@chu.fr", "bob@chu.fr"}
    assert repo.is_published(show_id) is False  # l'aperçu n'a rien publié

    # tirage définitif : une seule fois
    resultat = run_final_draw(participants, supply, seed=42)
    for a in resultat.assignments:
        if a.slot is not None:
            repo.add_listing(new_listing(
                show_id, ListingType.ECHANGE.value, a.participant_id, a.participant_name,
                offered_slot=a.slot, offered_count=a.count, wanted_groups=[], is_gift=False,
            ))
        repo.record_historique(show_id, a.participant_id, a.tier_index)
    repo.mark_published(show_id, "gest@chu.fr", seed=42, draw_order=resultat.draw_order)

    listings = repo.open_listings(show_id)
    assert len(listings) == 2  # Alice et Bob servis, Denis exclu (count=0)
    assert repo.is_published(show_id) is True
