from datetime import date, time

from models import new_listing, listing_label, run_daily_matching_cycle
from conftest import valid_slot


def test_propose_sur_un_depot_enregistre_la_reciproque(repo, show_id):
    s6 = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    s16 = valid_slot(repo, show_id, date(2026, 12, 16), time(17, 30))

    depot = new_listing(show_id, "echange", "ed@chu.fr", "Ed", offered_slot=s6, offered_count=2)
    proposeur = new_listing(show_id, "echange", "fab@chu.fr", "Fab", offered_slot=s16, offered_count=2)
    repo.add_listing(depot)
    repo.add_listing(proposeur)
    assert listing_label(depot) == "depot"

    ok, _ = repo.propose_exchange(show_id, proposeur.id, depot.id)
    assert ok

    depot2 = repo.get_listing(depot.id)
    proposeur2 = repo.get_listing(proposeur.id)
    assert depot2.wanted_groups[0].slots == [s16]
    assert proposeur2.wanted_groups[0].slots == [s6]
    assert listing_label(depot2) == "echange"

    _, props = run_daily_matching_cycle(repo, show_id)
    assert len(props) == 1


def test_propose_sur_offre_decidee_verifie_le_prerequis(repo, show_id):
    s6 = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    s16 = valid_slot(repo, show_id, date(2026, 12, 16), time(17, 30))
    s17 = valid_slot(repo, show_id, date(2026, 12, 17), time(10, 30))

    from models import new_wanted_group
    decidee = new_listing(show_id, "echange", "greg@chu.fr", "Greg", offered_slot=s6, offered_count=1,
                           wanted_groups=[new_wanted_group(1, [s17])])
    repo.add_listing(decidee)

    mauvais = new_listing(show_id, "echange", "hugo@chu.fr", "Hugo", offered_slot=s16, offered_count=1)
    repo.add_listing(mauvais)
    ok, _ = repo.propose_exchange(show_id, mauvais.id, decidee.id)
    assert not ok

    bon = new_listing(show_id, "echange", "ines@chu.fr", "Ines", offered_slot=s17, offered_count=1)
    repo.add_listing(bon)
    ok2, _ = repo.propose_exchange(show_id, bon.id, decidee.id)
    assert ok2

    decidee2 = repo.get_listing(decidee.id)
    assert len(decidee2.wanted_groups) == 1  # la cible déjà décidée n'est pas modifiée


def test_double_clic_sur_je_me_propose_sans_effet(repo, show_id):
    s6 = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    s5 = valid_slot(repo, show_id, date(2026, 12, 5), time(15, 0))

    depot = new_listing(show_id, "echange", "ed@chu.fr", "Ed", offered_slot=s6, offered_count=2)
    mien = new_listing(show_id, "echange", "fab@chu.fr", "Fab", offered_slot=s5, offered_count=2)
    repo.add_listing(depot)
    repo.add_listing(mien)

    ok_a, _ = repo.propose_exchange(show_id, mien.id, depot.id)
    ok_b, _ = repo.propose_exchange(show_id, mien.id, depot.id)

    assert ok_a and not ok_b
    assert len(repo.get_listing(mien.id).wanted_groups) == 1
