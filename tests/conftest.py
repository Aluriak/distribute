import pytest

from models import Repo, Slot, SlotStatus


@pytest.fixture
def repo():
    return Repo(":memory:")


@pytest.fixture
def show_id():
    return "noel-2026"


def valid_slot(repo, show_id, jour, heure, created_by="system"):
    """Créneau validé, prêt à l'emploi dans un test."""
    slot = Slot(jour, heure)
    repo.get_or_create_slot(show_id, slot, created_by)
    repo.set_slot_status(show_id, slot, SlotStatus.VALIDE)
    return slot
