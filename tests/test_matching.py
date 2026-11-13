from datetime import date, time

from models import (
    Slot, new_listing, new_wanted_group, SlotStatus,
    run_daily_matching_cycle,
)
from conftest import valid_slot


def test_echange_reciproque_simple(repo, show_id):
    """Un bilatéral n'est qu'un cycle de longueur 2 : UNE Proposition à
    deux étapes (A->B et B->A), pas un champ 'réciproque' séparé."""
    s6 = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    s16 = valid_slot(repo, show_id, date(2026, 12, 16), time(17, 30))
    bob = new_listing(show_id, "echange", "bob@chu.fr", "Bob", offered_slot=s6, offered_count=1,
                       wanted_groups=[new_wanted_group(1, [s16])])
    carla = new_listing(show_id, "echange", "carla@chu.fr", "Carla", offered_slot=s16, offered_count=1,
                         wanted_groups=[new_wanted_group(1, [s6])])
    repo.add_listing(bob)
    repo.add_listing(carla)

    _, props = run_daily_matching_cycle(repo, show_id)

    assert len(props) == 1
    p = props[0]
    assert len(p.steps) == 2
    assert set(p.participant_ids) == {bob.id, carla.id}


def test_don_satisfait_un_chercheur_sans_rien_offrir(repo, show_id):
    """Un don est une Proposition à UNE SEULE étape (pas de retour)."""
    s6 = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    seeker = new_listing(show_id, "recherche", "alice@chu.fr", "Alice", wanted_groups=[new_wanted_group(1, [s6])])
    don = new_listing(show_id, "echange", "bob@chu.fr", "Bob", offered_slot=s6, offered_count=1, is_gift=True)
    repo.add_listing(seeker)
    repo.add_listing(don)

    _, props = run_daily_matching_cycle(repo, show_id)

    assert len(props) == 1
    assert len(props[0].steps) == 1


def test_recherche_pure_ne_matche_pas_un_echange_exigeant(repo, show_id):
    """Un échange qui veut quelque chose en retour ne doit jamais être
    satisfait par un chercheur qui n'a rien à donner."""
    s6 = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    s16 = valid_slot(repo, show_id, date(2026, 12, 16), time(17, 30))
    seeker_pur = new_listing(show_id, "recherche", "alice@chu.fr", "Alice", wanted_groups=[new_wanted_group(1, [s6])])
    echange_exigeant = new_listing(show_id, "echange", "bob@chu.fr", "Bob", offered_slot=s6, offered_count=1,
                                    wanted_groups=[new_wanted_group(1, [s16])])
    repo.add_listing(seeker_pur)
    repo.add_listing(echange_exigeant)

    _, props = run_daily_matching_cycle(repo, show_id)

    assert props == []


def test_creneau_reciproque_non_valide_bloque_le_match(repo, show_id):
    """Le créneau RENDU en retour doit lui aussi être validé, pas
    seulement celui qu'on reçoit — sinon la file de modération est
    contournable."""
    s6 = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    s16 = Slot(date(2026, 12, 16), time(17, 30))
    repo.get_or_create_slot(show_id, s16, "bob@chu.fr")  # reste EN_ATTENTE

    bob = new_listing(show_id, "echange", "bob@chu.fr", "Bob", offered_slot=s16, offered_count=1,
                       wanted_groups=[new_wanted_group(1, [s6])])
    carla = new_listing(show_id, "echange", "carla@chu.fr", "Carla", offered_slot=s6, offered_count=1,
                         wanted_groups=[new_wanted_group(1, [s16])])
    repo.add_listing(bob)
    repo.add_listing(carla)

    _, props = run_daily_matching_cycle(repo, show_id)
    assert props == [], "s16 non validé ne doit pas pouvoir servir de retour"

    repo.set_slot_status(show_id, s16, SlotStatus.VALIDE)
    _, props2 = run_daily_matching_cycle(repo, show_id)
    assert len(props2) == 1


def test_groupes_multiples_matchent_independamment(repo, show_id):
    """4 places contre 2 sur un créneau ET 2 sur un autre — chaque groupe
    peut trouver un donneur différent, donc deux dons séparés ici."""
    s6 = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    s16 = valid_slot(repo, show_id, date(2026, 12, 16), time(17, 30))
    s17 = valid_slot(repo, show_id, date(2026, 12, 17), time(10, 30))

    demandeur = new_listing(
        show_id, "echange", "demandeur@chu.fr", "Demandeur",
        offered_slot=s6, offered_count=4,
        wanted_groups=[new_wanted_group(2, [s16]), new_wanted_group(2, [s17])],
    )
    don16 = new_listing(show_id, "echange", "don16@chu.fr", "Don16", offered_slot=s16, offered_count=2, is_gift=True)
    don17 = new_listing(show_id, "echange", "don17@chu.fr", "Don17", offered_slot=s17, offered_count=2, is_gift=True)
    repo.add_listing(demandeur)
    repo.add_listing(don16)
    repo.add_listing(don17)

    _, props = run_daily_matching_cycle(repo, show_id)

    assert len(props) == 2
    slots_obtenus = {Slot.from_key(p.steps[0][2]) for p in props}
    assert slots_obtenus == {s16, s17}


def test_wildcard_group_matche_nimporte_quel_slot_valide(repo, show_id):
    s12 = valid_slot(repo, show_id, date(2026, 12, 12), time(14, 0))
    seeker = new_listing(show_id, "recherche", "alice@chu.fr", "Alice", wanted_groups=[new_wanted_group(2)])
    giver = new_listing(show_id, "echange", "bob@chu.fr", "Bob", offered_slot=s12, offered_count=2, is_gift=True)
    repo.add_listing(seeker)
    repo.add_listing(giver)

    _, props = run_daily_matching_cycle(repo, show_id)

    assert len(props) == 1
    assert Slot.from_key(props[0].steps[0][2]) == s12


def test_transaction_realisee_ne_libere_jamais_la_capacite(repo, show_id):
    """Bug critique trouvé en revue : une fois RÉALISÉE, le ticket a
    réellement changé de main — il ne doit jamais redevenir disponible
    pour un troisième larron. Seule ANNULEE doit libérer la capacité."""
    s6 = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    bob = new_listing(show_id, "echange", "bob@chu.fr", "Bob", offered_slot=s6, offered_count=1, is_gift=True)
    alice = new_listing(show_id, "recherche", "alice@chu.fr", "Alice", wanted_groups=[new_wanted_group(1, [s6])])
    repo.add_listing(bob)
    repo.add_listing(alice)

    _, props = run_daily_matching_cycle(repo, show_id)
    p = props[0]
    repo.accept_proposition_step(p.id, bob.id)
    txns, _ = repo.accept_proposition_step(p.id, alice.id)
    t = txns[0]
    repo.confirm(t.id, "a")
    repo.confirm(t.id, "b")

    denis = new_listing(show_id, "recherche", "denis@chu.fr", "Denis", wanted_groups=[new_wanted_group(1, [s6])])
    repo.add_listing(denis)
    _, props2 = run_daily_matching_cycle(repo, show_id)

    assert props2 == []


def test_annonce_clôturee_sort_du_pool(repo, show_id):
    s6 = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    l = new_listing(show_id, "echange", "bob@chu.fr", "Bob", offered_slot=s6, offered_count=1, is_gift=True)
    repo.add_listing(l)
    repo.close_listing(l.id)

    assert repo.open_listings(show_id) == []
