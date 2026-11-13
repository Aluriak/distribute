from datetime import date, time

from models import (
    new_listing, new_wanted_group, TransactionStatus,
    find_exchange_cycles, run_daily_matching_cycle, PropositionStatus,
)
from conftest import valid_slot


def _cycle_a_trois(repo, show_id):
    sx = valid_slot(repo, show_id, date(2026, 12, 5), time(15, 0))
    sy = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    sz = valid_slot(repo, show_id, date(2026, 12, 12), time(14, 0))
    a = new_listing(show_id, "echange", "a@chu.fr", "A", offered_slot=sx, offered_count=1,
                     wanted_groups=[new_wanted_group(1, [sy])])
    b = new_listing(show_id, "echange", "b@chu.fr", "B", offered_slot=sy, offered_count=1,
                     wanted_groups=[new_wanted_group(1, [sz])])
    c = new_listing(show_id, "echange", "c@chu.fr", "C", offered_slot=sz, offered_count=1,
                     wanted_groups=[new_wanted_group(1, [sx])])
    repo.add_listing(a)
    repo.add_listing(b)
    repo.add_listing(c)
    return a, b, c


def test_cycle_a_trois_parties_trouve_par_find_exchange_cycles(repo, show_id):
    a, b, c = _cycle_a_trois(repo, show_id)
    listings = repo.open_listings(show_id)
    remaining_offered = {l.id: l.offered_count for l in listings if l.offered_slot}
    remaining_group = {g.id: g.count for l in listings for g in l.wanted_groups}
    valid_keys = {l.offered_slot.key() for l in listings}

    cycles = find_exchange_cycles(listings, valid_keys, remaining_offered, remaining_group, max_cycle_len=4)

    assert len(cycles) == 1
    assert len(cycles[0]) == 3


def test_bilateral_est_un_cycle_de_longueur_2(repo, show_id):
    s6 = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    s16 = valid_slot(repo, show_id, date(2026, 12, 16), time(17, 30))
    bob = new_listing(show_id, "echange", "bob@chu.fr", "Bob", offered_slot=s6, offered_count=1,
                       wanted_groups=[new_wanted_group(1, [s16])])
    carla = new_listing(show_id, "echange", "carla@chu.fr", "Carla", offered_slot=s16, offered_count=1,
                         wanted_groups=[new_wanted_group(1, [s6])])
    repo.add_listing(bob)
    repo.add_listing(carla)
    listings = repo.open_listings(show_id)
    remaining_offered = {l.id: l.offered_count for l in listings if l.offered_slot}
    remaining_group = {g.id: g.count for l in listings for g in l.wanted_groups}
    valid_keys = {s6.key(), s16.key()}

    cycles = find_exchange_cycles(listings, valid_keys, remaining_offered, remaining_group, max_cycle_len=2)

    assert len(cycles) == 1 and len(cycles[0]) == 2


def test_pas_de_cycle_a_3_sans_relever_la_longueur_configuree(repo, show_id):
    _cycle_a_trois(repo, show_id)
    _, props = run_daily_matching_cycle(repo, show_id)  # max_cycle_len=2 par défaut
    assert props == []


def test_longueur_configuree_debloque_le_cycle_a_3(repo, show_id):
    _cycle_a_trois(repo, show_id)
    ok, _ = repo.set_max_cycle_length(show_id, 4, ceiling=4)
    assert ok
    longueur = repo.get_max_cycle_length(show_id, ceiling=4)

    _, props = run_daily_matching_cycle(repo, show_id, max_cycle_len=longueur)

    assert len(props) == 1
    assert len(props[0].steps) == 3


def test_longueur_max_bornee_entre_3_et_le_plafond(repo, show_id):
    assert repo.get_max_cycle_length(show_id, ceiling=2) == 2  # désactivé par défaut

    ok, _ = repo.set_max_cycle_length(show_id, 3, ceiling=2)
    assert not ok, "le plafond de déploiement doit être >= 3 pour activer la fonctionnalité"

    ok2, _ = repo.set_max_cycle_length(show_id, 4, ceiling=4)
    assert ok2
    assert repo.get_max_cycle_length(show_id, ceiling=4) == 4

    ok3, _ = repo.set_max_cycle_length(show_id, 2, ceiling=4)
    assert not ok3, "le gestionnaire ne doit jamais pouvoir redescendre à 2 (= cycles désactivés)"


def test_cycle_a_n_suit_le_meme_engagement_individuel_que_le_bilateral(repo, show_id):
    a, b, c = _cycle_a_trois(repo, show_id)
    repo.set_max_cycle_length(show_id, 4, ceiling=4)
    longueur = repo.get_max_cycle_length(show_id, ceiling=4)
    _, props = run_daily_matching_cycle(repo, show_id, max_cycle_len=longueur)
    p = props[0]

    assert set(p.participant_ids) == {a.id, b.id, c.id}

    txns, p2 = repo.accept_proposition_step(p.id, a.id)
    assert txns is None
    txns, p2 = repo.accept_proposition_step(p.id, b.id)
    assert txns is None

    txns, p2 = repo.accept_proposition_step(p.id, c.id)
    assert txns is not None and len(txns) == 3
    assert p2.status == PropositionStatus.ACCEPTEE
    assert all(t.status == TransactionStatus.PROPOSEE for t in txns)


def test_un_seul_refus_defait_tout_le_cycle_a_n(repo, show_id):
    a, b, c = _cycle_a_trois(repo, show_id)
    repo.set_max_cycle_length(show_id, 4, ceiling=4)
    longueur = repo.get_max_cycle_length(show_id, ceiling=4)
    _, props = run_daily_matching_cycle(repo, show_id, max_cycle_len=longueur)
    p = props[0]

    repo.accept_proposition_step(p.id, a.id)
    repo.decline_proposition_step(p.id, b.id)

    assert repo.get_proposition(p.id).status == PropositionStatus.REFUSEE


def test_transactions_du_cycle_suivent_le_confirm_dispute_habituel(repo, show_id):
    a, b, c = _cycle_a_trois(repo, show_id)
    repo.set_max_cycle_length(show_id, 4, ceiling=4)
    longueur = repo.get_max_cycle_length(show_id, ceiling=4)
    _, props = run_daily_matching_cycle(repo, show_id, max_cycle_len=longueur)
    p = props[0]

    repo.accept_proposition_step(p.id, a.id)
    repo.accept_proposition_step(p.id, b.id)
    txns, _ = repo.accept_proposition_step(p.id, c.id)

    for t in txns:
        repo.confirm(t.id, "a")
        repo.confirm(t.id, "b")

    for t in txns:
        assert repo._get_transaction(t.id).status == TransactionStatus.REALISEE


def test_le_registre_partage_protege_un_cycle_en_attente(repo, show_id):
    """Le problème d'origine (deux comptabilités séparées) : un cycle à N
    en attente d'engagements doit bloquer la capacité même pour un
    recalcul de longueur différente (ex. bilatéral, longueur 2)."""
    a, b, c = _cycle_a_trois(repo, show_id)
    repo.set_max_cycle_length(show_id, 4, ceiling=4)
    _, props = run_daily_matching_cycle(repo, show_id, max_cycle_len=4)
    p = props[0]
    repo.accept_proposition_step(p.id, a.id)
    repo.accept_proposition_step(p.id, b.id)  # C n'a pas encore répondu

    denis = new_listing(show_id, "recherche", "denis@chu.fr", "Denis",
                         wanted_groups=[new_wanted_group(1, [a.offered_slot])])
    repo.add_listing(denis)
    _, props2 = run_daily_matching_cycle(repo, show_id, max_cycle_len=2)

    assert props2 == [], "la place de A, déjà engagée dans le cycle en attente, ne doit pas être repromise"
