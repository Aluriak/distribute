from datetime import date, time, datetime, timedelta

from models import (
    new_listing, new_wanted_group, PropositionStatus, TransactionStatus, ListingStatus,
    run_daily_matching_cycle,
)
from conftest import valid_slot


def _creer_proposition(repo, show_id):
    """Un don simple : une Proposition à une seule étape, deux
    participants (seeker, giver)."""
    s6 = valid_slot(repo, show_id, date(2026, 12, 6), time(10, 30))
    seeker = new_listing(show_id, "recherche", "alice@chu.fr", "Alice", wanted_groups=[new_wanted_group(1, [s6])])
    giver = new_listing(show_id, "echange", "bob@chu.fr", "Bob", offered_slot=s6, offered_count=1, is_gift=True)
    repo.add_listing(seeker)
    repo.add_listing(giver)
    _, props = run_daily_matching_cycle(repo, show_id)
    return seeker, giver, props[0]


def test_mise_en_relation_exige_tous_les_engagements(repo, show_id):
    seeker, giver, p = _creer_proposition(repo, show_id)

    txns, _ = repo.accept_proposition_step(p.id, giver.id)
    assert txns is None, "pas de transaction tant que tous n'ont pas accepté"

    txns, _ = repo.accept_proposition_step(p.id, seeker.id)
    assert txns is not None and len(txns) == 1
    assert txns[0].status == TransactionStatus.PROPOSEE


def test_refus_tue_la_proposition_meme_apres_engagement_prealable(repo, show_id):
    seeker, giver, p = _creer_proposition(repo, show_id)
    repo.accept_proposition_step(p.id, giver.id)
    repo.decline_proposition_step(p.id, seeker.id)

    assert repo.get_proposition(p.id).status == PropositionStatus.REFUSEE


def test_decline_avec_annonce_obsolete_la_cloture(repo, show_id):
    seeker, giver, p = _creer_proposition(repo, show_id)
    repo.decline_proposition_step(p.id, giver.id, listing_no_longer_relevant=True)

    assert repo.get_listing(giver.id).status == ListingStatus.CLOTUREE


def test_expiration_puis_recalcul(repo, show_id):
    seeker, giver, p = _creer_proposition(repo, show_id)
    repo.conn.execute(
        "UPDATE propositions SET created_at=? WHERE id=?",
        ((datetime.now() - timedelta(hours=25)).isoformat(), p.id),
    )
    repo.conn.commit()

    expired, created = run_daily_matching_cycle(repo, show_id)

    assert len(expired) == 1 and expired[0].id == p.id
    assert len(created) == 1  # recalcul : toujours le même (unique) donneur dispo


def test_confirm_dispute_auto_validation_contestation(repo, show_id):
    seeker, giver, p = _creer_proposition(repo, show_id)
    repo.accept_proposition_step(p.id, seeker.id)
    txns, _ = repo.accept_proposition_step(p.id, giver.id)
    txn = txns[0]

    repo.confirm(txn.id, "a")
    repo.conn.execute(
        "UPDATE transactions SET confirmed_a_at=? WHERE id=?",
        ((datetime.now() - timedelta(days=2)).isoformat(), txn.id),
    )
    repo.conn.commit()

    validated = repo.run_auto_validation(show_id, timedelta(days=1))
    assert len(validated) == 1
    t = repo._get_transaction(txn.id)
    assert t.status == TransactionStatus.REALISEE and t.auto_side == "b"

    ok, t2 = repo.contest(txn.id, "b", grace=timedelta(days=1))
    assert ok and t2.status == TransactionStatus.LITIGE


def test_resolve_litige_refuse_un_second_verdict(repo, show_id):
    seeker, giver, p = _creer_proposition(repo, show_id)
    repo.accept_proposition_step(p.id, seeker.id)
    txns, _ = repo.accept_proposition_step(p.id, giver.id)
    txn = txns[0]
    repo.dispute(txn.id, "a")

    assert repo.resolve_litige(txn.id, "mod@chu.fr", TransactionStatus.REALISEE) is True
    assert repo.resolve_litige(txn.id, "mod2@chu.fr", TransactionStatus.ANNULEE) is False
    assert repo._get_transaction(txn.id).status == TransactionStatus.REALISEE
