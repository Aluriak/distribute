from datetime import date, time

from models import Slot
from allocation import Participant, SlotSupply, run_final_draw, preview_probabilities


def test_tirage_reproductible_independamment_de_lordre_dentree():
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s6, 1)]
    alice = Participant("a@chu.fr", "Alice", 1, tiers=[[s6]])
    bob = Participant("b@chu.fr", "Bob", 1, tiers=[[s6]])

    r1 = run_final_draw([alice, bob], supply, seed=42)
    r2 = run_final_draw([bob, alice], supply, seed=42)

    assert r1.draw_order == r2.draw_order


def test_graines_differentes_donnent_des_ordres_differents():
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s6, 1)]
    alice = Participant("a@chu.fr", "Alice", 1, tiers=[[s6]])
    bob = Participant("b@chu.fr", "Bob", 1, tiers=[[s6]])

    ordres = {tuple(run_final_draw([alice, bob], supply, seed=s).draw_order) for s in range(20)}

    assert len(ordres) > 1


def test_creneau_non_dispute_cent_pourcent_pour_tous():
    s16 = Slot(date(2026, 12, 16), time(17, 30))
    supply = [SlotSupply(s16, 10)]
    alice = Participant("a@chu.fr", "Alice", 1, tiers=[[s16]])
    bob = Participant("b@chu.fr", "Bob", 1, tiers=[[s16]])

    probas = preview_probabilities([alice, bob], supply, n_simulations=100)

    assert probas["a@chu.fr"][s16.key()] == 1.0
    assert probas["b@chu.fr"][s16.key()] == 1.0


def test_creneau_dispute_probabilites_entre_0_et_1():
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s6, 1)]
    alice = Participant("a@chu.fr", "Alice", 1, tiers=[[s6]])
    bob = Participant("b@chu.fr", "Bob", 1, tiers=[[s6]])

    probas = preview_probabilities([alice, bob], supply, n_simulations=300)
    pa = probas["a@chu.fr"].get(s6.key(), 0.0)
    pb = probas["b@chu.fr"].get(s6.key(), 0.0)

    assert 0.0 < pa < 1.0 and 0.0 < pb < 1.0
    assert abs((pa + pb) - 1.0) < 0.15


def test_poids_historique_ameliore_statistiquement_sans_garantir():
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s6, 1)]
    alice = Participant("a@chu.fr", "Alice", 1, tiers=[[s6]], priority_weight=3.0)
    bob = Participant("b@chu.fr", "Bob", 1, tiers=[[s6]], priority_weight=1.0)

    probas = preview_probabilities([alice, bob], supply, n_simulations=1000)

    assert probas["a@chu.fr"][s6.key()] > probas["b@chu.fr"][s6.key()]


def test_jamais_deux_creneaux_meme_si_les_deux_premiers_choix_sont_tendus():
    """Cas posé par l'utilisateur : quelqu'un veut A (très demandé) ET B
    (très demandé) ET C (peu demandé), dans cet ordre de préférence. Il ne
    peut JAMAIS obtenir A et B à la fois — l'attribution sérielle
    s'arrête dès le premier succès."""
    sA = Slot(date(2026, 12, 6), time(10, 30))
    sB = Slot(date(2026, 12, 16), time(17, 30))
    sC = Slot(date(2026, 12, 20), time(14, 0))
    supply = [SlotSupply(sA, 1), SlotSupply(sB, 1), SlotSupply(sC, 10)]

    alice = Participant("alice@chu.fr", "Alice", 1, tiers=[[sA], [sB], [sC]])
    autres = [Participant(f"p{i}@chu.fr", f"P{i}", 1, tiers=[[sA], [sB]]) for i in range(4)]

    for seed in range(50):
        resultat = run_final_draw([alice] + autres, supply, seed=seed)
        alice_slots = [a.slot for a in resultat.assignments if a.participant_id == "alice@chu.fr"]
        assert len(alice_slots) == 1  # une seule entrée d'assignation, jamais deux
        # et elle n'apparaît jamais deux fois dans l'ordre tiré
        assert resultat.draw_order.count("alice@chu.fr") == 1


def test_creneau_non_dispute_toujours_garanti_quel_que_soit_lordre():
    """Partie 'ne remplace pas la résolution' : C (peu demandé) reste à
    100% de chances quel que soit l'ordre de tirage, même pour la
    dernière personne servie."""
    sA = Slot(date(2026, 12, 6), time(10, 30))
    sC = Slot(date(2026, 12, 20), time(14, 0))
    supply = [SlotSupply(sA, 1), SlotSupply(sC, 10)]
    participants = [Participant(f"p{i}@chu.fr", f"P{i}", 1, tiers=[[sA], [sC]]) for i in range(5)]

    for seed in range(20):
        resultat = run_final_draw(participants, supply, seed=seed)
        assert all(a.slot is not None for a in resultat.assignments)  # tout le monde servi
        assert sum(1 for a in resultat.assignments if a.slot == sA) == 1  # A : un seul gagnant
        assert sum(1 for a in resultat.assignments if a.slot == sC) == 4  # C : tous les autres, sans tension


def test_aucune_scission_un_seul_creneau_par_participant():
    s5 = Slot(date(2026, 12, 5), time(15, 0))
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s5, 1), SlotSupply(s6, 1)]
    alice = Participant("a@chu.fr", "Alice", 2, tiers=[[]])  # veut 2 places, n'importe où

    resultat = run_final_draw([alice], supply, seed=1)

    # aucun créneau n'a 2 places dispo -> Alice doit être non servie, pas scindée
    assert resultat.assignments[0].slot is None
