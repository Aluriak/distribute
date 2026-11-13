from datetime import date, time

from models import Slot
from allocation import Participant, SlotSupply, solve_egalitarian, compute_priority_weight, default_unserved_penalty


def test_conflit_resolu_par_minimax():
    s5 = Slot(date(2026, 12, 5), time(15, 0))
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s5, 1), SlotSupply(s6, 1)]
    alice = Participant("a@chu.fr", "Alice", 1, tiers=[[s5], [s6]])
    bob = Participant("b@chu.fr", "Bob", 1, tiers=[[s5], [s6]])

    assignments, pire_rang = solve_egalitarian([alice, bob], supply)

    ranks = sorted(a.tier_index for a in assignments)
    assert ranks == [0, 1]
    assert pire_rang == 1


def test_penurie_signalee_proprement():
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s6, 1)]
    alice = Participant("a@chu.fr", "Alice", 1, tiers=[[s6]])
    bob = Participant("b@chu.fr", "Bob", 1, tiers=[[s6]])

    assignments, _ = solve_egalitarian([alice, bob], supply)

    servis = [a for a in assignments if a.slot is not None]
    non_servis = [a for a in assignments if a.slot is None]
    assert len(servis) == 1 and len(non_servis) == 1


def test_participant_compte_zero_est_ignore():
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s6, 1)]
    personne = Participant("x@chu.fr", "X", 0, tiers=[])

    assignments, _ = solve_egalitarian([personne], supply)

    assert assignments == []


def test_priorite_historique_fait_pencher_un_conflit():
    s5 = Slot(date(2026, 12, 5), time(15, 0))
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s5, 1), SlotSupply(s6, 1)]
    alice = Participant("a@chu.fr", "Alice", 1, tiers=[[s5], [s6]], priority_weight=2.0)
    bob = Participant("b@chu.fr", "Bob", 1, tiers=[[s5], [s6]], priority_weight=1.0)

    assignments, _ = solve_egalitarian([alice, bob], supply)

    alice_tier = next(a.tier_index for a in assignments if a.participant_name == "Alice")
    assert alice_tier == 0


def test_compute_priority_weight():
    assert compute_priority_weight([], unserved_penalty=3) == 1.0
    assert compute_priority_weight([2, None], unserved_penalty=3) == 1.0 + (2 + 3) / 2


def test_default_unserved_penalty():
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    p1 = Participant("a@chu.fr", "A", 1, tiers=[[s6]])
    p2 = Participant("b@chu.fr", "B", 1, tiers=[[s6], [], []])
    assert default_unserved_penalty([p1, p2]) == 3
