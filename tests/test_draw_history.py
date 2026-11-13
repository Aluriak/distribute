import pytest
from datetime import date, time

from models import Slot
from allocation import (
    Participant, SlotSupply, run_draw_iteration, list_draws, _derive_seed, _base_seed,
)


def test_justification_obligatoire(repo, show_id):
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s6, 1)]
    alice = Participant("a@chu.fr", "Alice", 1, tiers=[[s6]])

    with pytest.raises(ValueError):
        run_draw_iteration(repo, show_id, [alice], supply, "", "gest@chu.fr")
    with pytest.raises(ValueError):
        run_draw_iteration(repo, show_id, [alice], supply, "   ", "gest@chu.fr")


def test_graines_derivees_de_la_meme_base(repo, show_id):
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s6, 1)]
    alice = Participant("a@chu.fr", "Alice", 1, tiers=[[s6]])
    bob = Participant("b@chu.fr", "Bob", 1, tiers=[[s6]])

    d1 = run_draw_iteration(repo, show_id, [alice, bob], supply, "Tirage initial", "gest@chu.fr")
    d2 = run_draw_iteration(repo, show_id, [alice, bob], supply, "Relance pour raison X", "gest@chu.fr")

    base = _base_seed(repo, show_id)
    assert d1.iteration == 1 and d2.iteration == 2
    assert d1.seed == _derive_seed(base, 1)
    assert d2.seed == _derive_seed(base, 2)
    assert d1.seed != d2.seed


def test_historique_complet_et_public(repo, show_id):
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s6, 1)]
    alice = Participant("a@chu.fr", "Alice", 1, tiers=[[s6]])

    run_draw_iteration(repo, show_id, [alice], supply, "Premier essai", "gest@chu.fr")
    run_draw_iteration(repo, show_id, [alice], supply, "Deuxième essai", "gest@chu.fr")

    historique = list_draws(repo, show_id)
    assert [h.iteration for h in historique] == [1, 2]
    assert [h.justification for h in historique] == ["Premier essai", "Deuxième essai"]


def test_aucun_tirage_possible_une_fois_publie(repo, show_id):
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s6, 1)]
    alice = Participant("a@chu.fr", "Alice", 1, tiers=[[s6]])

    d1 = run_draw_iteration(repo, show_id, [alice], supply, "Tirage initial", "gest@chu.fr")
    repo.mark_published(show_id, "gest@chu.fr", d1.seed, d1.draw_order, iteration=d1.iteration)

    with pytest.raises(RuntimeError):
        run_draw_iteration(repo, show_id, [alice], supply, "Nouvelle tentative", "gest@chu.fr")


def test_iteration_publiee_indique_le_nombre_de_relances(repo, show_id):
    s6 = Slot(date(2026, 12, 6), time(10, 30))
    supply = [SlotSupply(s6, 1)]
    alice = Participant("a@chu.fr", "Alice", 1, tiers=[[s6]])

    run_draw_iteration(repo, show_id, [alice], supply, "Essai 1", "gest@chu.fr")
    run_draw_iteration(repo, show_id, [alice], supply, "Essai 2", "gest@chu.fr")
    d3 = run_draw_iteration(repo, show_id, [alice], supply, "Essai 3, celui-ci est bon", "gest@chu.fr")
    repo.mark_published(show_id, "gest@chu.fr", d3.seed, d3.draw_order, iteration=d3.iteration)

    _, _, _, _, iteration = repo.get_publication(show_id)
    assert iteration == 3  # donc 2 relances avant la publication
