"""échange-billet — app NiceGUI (auth OIDC via appacl, sur le modèle de skel).

Rôles Keycloak configurables par variable d'environnement — aucun nom de
rôle n'est codé en dur :
- extérieur    : aucun rôle applicatif (informatif, non vérifié activement).
- utilisateur  : peut consulter, poster, répondre aux propositions.
- modérateur   : en plus, valide les créneaux saisis par les utilisateurs
  et tranche les litiges.
- gestionnaire : rôle à part (pas un niveau au-dessus de modérateur — on
  l'a souvent EN PLUS, mais ce n'est pas automatique) : seul lui peut
  définir les créneaux du spectacle et leur nombre de billets, et
  déclencher/relancer le calcul de répartition initiale.
"""
import os
from datetime import date, time, timedelta

import yaml
from nicegui import app, ui
from cdctools import appacl

from models import (
    Slot, ListingType, Repo, SlotStatus, TransactionStatus,
    new_listing, new_wanted_group, listing_label,
)
from allocation import (
    Participant, SlotSupply, compute_priority_weight, default_unserved_penalty,
    preview_probabilities, run_draw_iteration, list_draws,
)
import notif

SHOW_ID = os.environ.get("SHOW_ID", "noel-2026")
SHOW_LABEL = os.environ.get("SHOW_LABEL", "Spectacle de Noël 2026")
DB_PATH = os.environ.get("ECHANGE_BILLET_DB", "echange-billet.db")
CONTEST_GRACE = timedelta(days=int(os.environ.get("CONTEST_GRACE_DAYS", "1")))

ROLE_EXTERIEUR = os.environ.get("ROLE_EXTERIEUR", "")
ROLE_UTILISATEUR = os.environ.get("ROLE_UTILISATEUR", "echange-billet-utilisateur")
ROLE_MODERATEUR = os.environ.get("ROLE_MODERATEUR", "echange-billet-moderateur")
ROLE_GESTIONNAIRE = os.environ.get("ROLE_GESTIONNAIRE", "echange-billet-gestionnaire")


def _load_config() -> dict:
    """Sur le modèle de skel (CONFIG_DIR/config.yml). Clés lues ici :
    - max_cycle_length_ceiling (int, défaut 2 = cycles à N parties
      désactivés) : plafond fixé au déploiement, que le gestionnaire ne
      peut que réduire (jamais dépasser) depuis /gestion.
    - moderateurs (liste d'e-mails) : pour notifier directement les
      modérateurs quand un litige s'ouvre (leur liste est connue, pas
      besoin de l'API d'administration Keycloak)."""
    config_dir = os.environ.get("CONFIG_DIR")
    if not config_dir:
        return {}
    path = os.path.join(config_dir, "config.yml")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


CONFIG = _load_config()
MAX_CYCLE_LENGTH_CEILING = int(CONFIG.get("max_cycle_length_ceiling", 2))
MODERATEUR_EMAILS = CONFIG.get("moderateurs", [])

repo = Repo(DB_PATH)


def get_roles(user_info: dict) -> list[str]:
    roles = list(user_info.get("realm_access", {}).get("roles", []))
    for client_roles in user_info.get("resource_access", {}).values():
        roles += client_roles.get("roles", [])
    return roles


def role_tier(user_info: dict) -> str:
    roles = get_roles(user_info)
    if ROLE_MODERATEUR and ROLE_MODERATEUR in roles:
        return "moderateur"
    if ROLE_UTILISATEUR and ROLE_UTILISATEUR in roles:
        return "utilisateur"
    return "exterieur"


def require_tier(user_info: dict, minimum: str) -> bool:
    order = {"exterieur": 0, "utilisateur": 1, "moderateur": 2}
    return order[role_tier(user_info)] >= order[minimum]


def is_gestionnaire(user_info: dict) -> bool:
    """Indépendant de l'échelle utilisateur/modérateur : un droit à part,
    qu'on attribue généralement EN PLUS d'un autre rôle, pas à la place."""
    return bool(ROLE_GESTIONNAIRE) and ROLE_GESTIONNAIRE in get_roles(user_info)


def access_denied(required_role: str) -> None:
    ui.label("Accès réservé.").classes("text-lg font-semibold")
    ui.label(f"Demandez à un administrateur le rôle Keycloak « {required_role} ».").classes("text-gray-500")


def post_listing(show_id, type_, user_info, offered, offered_count, wanted_groups, is_gift, note) -> str:
    all_slots = ([offered] if offered else []) + [s for g in wanted_groups for s in g.slots]
    nouveaux = []
    for s in all_slots:
        status = repo.get_or_create_slot(show_id, s, user_info["email"])
        if status == SlotStatus.EN_ATTENTE:
            nouveaux.append(s)

    listing = new_listing(
        show_id=show_id, type_=type_, user_email=user_info["email"],
        user_name=user_info.get("name", user_info["email"]),
        offered_slot=offered, offered_count=offered_count, wanted_groups=wanted_groups,
        is_gift=is_gift, note=note,
    )
    repo.add_listing(listing)

    if not is_gift and offered and not wanted_groups:
        msg = ("Annonce publiée en dépôt : vos places sont visibles, sans contrepartie précisée. "
               "D'autres utilisateurs peuvent se proposer, ou vous pouvez attendre.")
    else:
        msg = "Annonce publiée. Le prochain calcul d'échange (midi) déterminera les correspondances possibles."
    if nouveaux:
        msg += (
            f" {len(nouveaux)} créneau(x) saisi(s) ne figurai(en)t pas encore au catalogue : "
            "en attente de validation par un modérateur avant de pouvoir matcher."
        )
    return msg


@ui.page("/")
def index():
    user_info = appacl.get_user_info(app)
    if not user_info:
        ui.button("SE CONNECTER", on_click=lambda: ui.navigate.to("/login"))
        return
    if not require_tier(user_info, "utilisateur"):
        access_denied(ROLE_UTILISATEUR)
        return

    with ui.column().classes("w-full max-w-3xl mx-auto gap-4 p-4"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label(SHOW_LABEL).classes("text-xl font-bold")
            with ui.row().classes("gap-2"):
                if require_tier(user_info, "moderateur"):
                    ui.button("Modération", on_click=lambda: ui.navigate.to("/moderation")).props("flat")
                if is_gestionnaire(user_info):
                    ui.button("Gestion", on_click=lambda: ui.navigate.to("/gestion")).props("flat")
                ui.button("Mes échanges", on_click=lambda: ui.navigate.to("/mes-echanges")).props("flat")
                ui.button("Se déconnecter", on_click=lambda: ui.navigate.to("/logout")).props("flat")

        ui.button("+ Poster une annonce", on_click=lambda: ui.navigate.to("/poster")).props("color=primary")
        ui.button(
            "Indiquer mes préférences pour la répartition initiale",
            on_click=lambda: ui.navigate.to("/preferences"),
        ).props("flat")
        ui.button(
            "Historique des tirages au sort",
            on_click=lambda: ui.navigate.to("/historique-tirages"),
        ).props("flat")

        reserved_ids = {
            l_id for t in repo.active_transactions(SHOW_ID) for l_id in (t.listing_a_id, t.listing_b_id)
        } | {
            l_id for p in repo.active_propositions(SHOW_ID) for l_id in p.participant_ids
        }
        ui.label("Annonces ouvertes").classes("text-lg font-semibold mt-4")
        for l in repo.open_listings(SHOW_ID):
            reserved = l.id in reserved_ids
            with ui.card().classes("w-full" + (" opacity-50" if reserved else "")):
                kind = listing_label(l)
                if kind == "recherche":
                    txt = "🔍 Cherche : " + " + ".join(str(g) for g in l.wanted_groups)
                elif kind == "don":
                    txt = f"🎁 Donne {l.offered_count} place(s) — {l.offered_slot}"
                elif kind == "depot":
                    txt = f"📦 A {l.offered_count} place(s) ({l.offered_slot}) — contrepartie pas encore précisée"
                else:
                    txt = (f"🔁 Libère {l.offered_count} place(s) ({l.offered_slot}) contre : "
                           + " + ".join(str(g) for g in l.wanted_groups))
                ui.label(txt)
                suffix = " · en cours de mise en relation" if reserved else ""
                ui.label(f"{l.user_name} — posté le {l.created_at:%d/%m}{suffix}").classes("text-sm text-gray-500")

                if not reserved and kind in ("depot", "echange") and l.user_email != user_info["email"]:
                    candidats = repo.eligible_listings_for(SHOW_ID, user_info["email"], l)
                    if candidats:
                        mien = candidats[0]  # simplification : la première annonce éligible trouvée

                        def make_propose(my_id=mien.id, target_id=l.id):
                            def _propose():
                                ok, msg = repo.propose_exchange(SHOW_ID, my_id, target_id)
                                ui.notify(msg, type="positive" if ok else "negative")
                                ui.navigate.to("/")
                            return _propose

                        ui.button(
                            f"Je me propose (avec mon ticket du {mien.offered_slot.jour:%d/%m})",
                            on_click=make_propose(),
                        ).props("flat dense color=primary").classes("mt-1")


@ui.page("/poster")
def poster():
    user_info = appacl.get_user_info(app)
    if not user_info:
        ui.navigate.to("/login")
        return
    if not require_tier(user_info, "utilisateur"):
        access_denied(ROLE_UTILISATEUR)
        return

    type_select = ui.select(
        {t.value: t.name.capitalize() for t in ListingType}, value=ListingType.ECHANGE.value,
        label="Type d'annonce",
    ).classes("w-full")

    with ui.row().classes("w-full gap-2").bind_visibility_from(
        type_select, "value", backward=lambda v: v == ListingType.ECHANGE.value
    ):
        offered_date = ui.date(label="Date libérée").classes("flex-1")
        offered_time = ui.time(label="Heure libérée").classes("flex-1")
        offered_count_input = ui.number("Nombre de places libérées", value=1, min=1, max=20).classes("flex-1")

    contrepartie = ui.radio(
        {"don": "Don — aucune contrepartie", "echange": "Échange — je sais déjà ce que je veux",
         "depot": "Échange — pas encore décidé, je verrai les offres"},
        value="echange",
    ).props("inline").bind_visibility_from(type_select, "value", backward=lambda v: v == ListingType.ECHANGE.value)
    ui.label(
        "Vos places restent visibles sans contrepartie fixée ; d'autres utilisateurs pourront se "
        "proposer, ou vous pourrez préciser vos critères plus tard."
    ).classes("text-xs text-gray-500 italic").bind_visibility_from(
        contrepartie, "value", backward=lambda v: v == "depot"
    )

    ui.label("Places recherchées — un groupe par lot de places ne devant pas forcément être sur le même créneau") \
        .bind_visibility_from(contrepartie, "value", backward=lambda v: v == "echange")
    groups_container = ui.column().classes("w-full gap-2")
    groups_state = []

    def add_group():
        group_index = len(groups_state) + 1
        with groups_container:
            with ui.card().classes("w-full bg-gray-50"):
                ui.label(f"Groupe {group_index}").classes("font-semibold")
                count_input = ui.number("Nombre de places", value=1, min=1, max=20).classes("w-full")
                wildcard_cb = ui.checkbox("Peu importe le créneau (parmi ceux validés)")
                slot_rows_col = ui.column().classes("w-full gap-1").bind_visibility_from(
                    wildcard_cb, "value", backward=lambda v: not v
                )
                slot_rows = []

                def add_slot_row():
                    with slot_rows_col:
                        with ui.row().classes("w-full gap-2"):
                            d = ui.date(label=f"Préférence {len(slot_rows)+1}").classes("flex-1")
                            t = ui.time(label="Heure").classes("flex-1")
                    slot_rows.append((d, t))

                add_slot_row()
                ui.button("+ créneau alternatif", on_click=add_slot_row).props("flat dense")
        groups_state.append({"count": count_input, "wildcard": wildcard_cb, "slot_rows": slot_rows})

    add_group()
    ui.button("+ ajouter un groupe de places recherchées", on_click=add_group).props("flat") \
        .bind_visibility_from(contrepartie, "value", backward=lambda v: v == "echange")
    groups_container.bind_visibility_from(contrepartie, "value", backward=lambda v: v == "echange")

    note_input = ui.textarea("Note (optionnel)").classes("w-full")
    feedback = ui.label().classes("text-sm")

    def submit():
        offered = None
        offered_count = 0
        if type_select.value == ListingType.ECHANGE.value and offered_date.value and offered_time.value:
            offered = Slot(date.fromisoformat(offered_date.value), time.fromisoformat(offered_time.value))
            offered_count = int(offered_count_input.value)

        is_gift = type_select.value == ListingType.ECHANGE.value and contrepartie.value == "don"
        wanted_groups = []
        if type_select.value == ListingType.RECHERCHE.value or contrepartie.value == "echange":
            for g in groups_state:
                count = int(g["count"].value)
                if g["wildcard"].value:
                    wanted_groups.append(new_wanted_group(count, []))
                else:
                    slots = [
                        Slot(date.fromisoformat(d.value), time.fromisoformat(t.value))
                        for d, t in g["slot_rows"] if d.value and t.value
                    ]
                    if slots:
                        wanted_groups.append(new_wanted_group(count, slots))

        if type_select.value == ListingType.RECHERCHE.value and not wanted_groups:
            feedback.text = "Indique au moins un groupe de places recherchées."
            return
        if type_select.value == ListingType.ECHANGE.value and not offered:
            feedback.text = "Indique le créneau que tu libères."
            return
        if type_select.value == ListingType.ECHANGE.value and contrepartie.value == "echange" and not wanted_groups:
            feedback.text = "Indique au moins un groupe de places recherchées, ou choisis une autre option."
            return

        msg = post_listing(
            SHOW_ID, type_select.value, user_info, offered, offered_count, wanted_groups, is_gift, note_input.value,
        )
        ui.notify(msg, type="positive")
        ui.navigate.to("/")

    ui.button("Publier", on_click=submit).props("color=primary")
    ui.button("Annuler", on_click=lambda: ui.navigate.to("/")).props("flat")


@ui.page("/mes-echanges")
def mes_echanges():
    user_info = appacl.get_user_info(app)
    if not user_info:
        ui.navigate.to("/login")
        return
    if not require_tier(user_info, "utilisateur"):
        access_denied(ROLE_UTILISATEUR)
        return
    email = user_info["email"]

    with ui.column().classes("w-full max-w-3xl mx-auto gap-4 p-4"):
        ui.label("Mes échanges").classes("text-xl font-bold")
        ui.button("← Retour", on_click=lambda: ui.navigate.to("/")).props("flat")

        # -- Propositions (générique : don, bilatéral, cycle à N — même mécanisme) --
        mes_propositions = repo.propositions_for_user(SHOW_ID, email)
        a_decider = [(p, lid) for p, lid in mes_propositions if p.accepted.get(lid) is None]
        en_attente_autre = [(p, lid) for p, lid in mes_propositions if p.accepted.get(lid) is not None]

        if a_decider:
            ui.label("Propositions reçues — à vous de décider").classes("text-lg font-semibold mt-2")
        for p, mon_id in a_decider:
            n = len(p.participant_ids)
            autres = n - 1
            mon_don = next((s for s in p.steps if s[0] == mon_id), None)   # j'y donne
            ma_reception = next((s for s in p.steps if s[1] == mon_id), None)  # j'y reçois

            with ui.card().classes("w-full border-l-4 border-amber-500"):
                if ma_reception:
                    _, _, slot_key, count, _ = ma_reception
                    ui.label(f"Vous recevriez {count} place(s) de {Slot.from_key(slot_key)}.")
                if mon_don:
                    _, receveur_id, slot_key, count, _ = mon_don
                    receveur = repo.get_listing(receveur_id)
                    ui.label(f"En contrepartie, vous donneriez {count} place(s) de {Slot.from_key(slot_key)} "
                             f"à {receveur.user_name}.")
                ui.label(f"Expire le {p.created_at + timedelta(hours=24):%d/%m à %Hh%M} si pas de réponse.") \
                    .classes("text-sm text-gray-500")

                if n > 2:
                    ui.label(
                        f"⚠️ Attention : {autres} autre(s) personne(s) compte(nt) sur vous. Si vous acceptez "
                        f"puis ne donnez pas vos places comme prévu, vous les mettez dans l'embarras — tout "
                        f"le cycle repose sur ce que chacun tient parole."
                    ).classes("text-sm text-orange-700")
                else:
                    ui.label(
                        "En acceptant, vous confirmez être toujours partant(e) et vous engagez à réaliser "
                        "l'échange avec la personne avec qui vous allez être mis en relation. La mise en "
                        "relation n'a lieu que lorsque TOUTES les parties ont accepté."
                    ).classes("text-xs text-gray-500 italic mt-1")

                def make_accept(pid=p.id, lid=mon_id, n=n):
                    def _accept():
                        txns, p_result = repo.accept_proposition_step(pid, lid)
                        if txns is not None:
                            avertissement = (
                                f" ({n} personnes engagées au total.)" if n > 2 else ""
                            )
                            for t in txns:
                                a, b = repo.get_listing(t.listing_a_id), repo.get_listing(t.listing_b_id)
                                notif.notify(
                                    [a.user_email, b.user_email],
                                    f"Mise en relation pour {t.count} place(s) de {t.slot} : "
                                    f"{a.user_name} <{a.user_email}> / {b.user_name} <{b.user_email}>."
                                    f"{avertissement} Organisez l'échange entre vous, puis confirmez-le "
                                    f"dans l'appli une fois fait.",
                                )
                            ui.notify("Tout le monde est engagé — mise en relation effectuée.", type="positive")
                        elif p_result.status.value == "echouee":
                            ui.notify(
                                "Les disponibilités ont changé entre-temps, cette proposition ne peut plus "
                                "aboutir.", type="negative",
                            )
                        else:
                            ui.notify(
                                "Engagement enregistré — en attente des autres participants.", type="positive",
                            )
                        ui.navigate.to("/mes-echanges")
                    return _accept

                def make_decline(pid=p.id, lid=mon_id, listing_no_longer_relevant=False):
                    def _decline():
                        repo.decline_proposition_step(pid, lid, listing_no_longer_relevant=listing_no_longer_relevant)
                        ui.navigate.to("/mes-echanges")
                    return _decline

                label_accept = (
                    f"Je m'engage à échanger mon ticket avec {autres} autre(s) personne(s)"
                    if n > 2 else "Je confirme et je m'engage"
                )
                not_available_label = "Je n'ai plus/besoin de ces places"
                with ui.row().classes("gap-2 mt-2"):
                    ui.button(label_accept, on_click=make_accept()).props("color=primary")
                    ui.button("Pas avec cette personne", on_click=make_decline()).props("flat")
                    ui.button(not_available_label, on_click=make_decline(listing_no_longer_relevant=True)).props(
                        "flat color=negative"
                    )

        if en_attente_autre:
            ui.label("En attente de l'engagement des autres participants").classes("text-lg font-semibold mt-4")
        for p, mon_id in en_attente_autre:
            with ui.card().classes("w-full"):
                for giver_id, receiver_id, slot_key, count, _ in p.steps:
                    if giver_id == mon_id or receiver_id == mon_id:
                        ui.label(f"{count} place(s) — {Slot.from_key(slot_key)}")
                ui.label(f"Vous êtes engagé(e) ; réponse attendue avant le "
                         f"{p.created_at + timedelta(hours=24):%d/%m à %Hh%M}").classes("text-sm text-gray-500")

        txns = repo.transactions_for_user(SHOW_ID, email)
        if txns:
            ui.label("Échanges mis en relation").classes("text-lg font-semibold mt-4")
        for t in txns:
            la, lb = repo.get_listing(t.listing_a_id), repo.get_listing(t.listing_b_id)
            is_a = la.user_email == email
            other = lb if is_a else la
            side = "a" if is_a else "b"
            already_confirmed = (t.confirmed_a_at if is_a else t.confirmed_b_at) is not None

            with ui.card().classes("w-full"):
                ui.label(f"{t.count} place(s) — {t.slot}")
                ui.label(f"Avec : {other.user_name} <{other.user_email}>").classes("text-sm text-gray-500")

                if t.status == TransactionStatus.LITIGE:
                    ui.label("⚠️ En litige, un modérateur va trancher.").classes("text-sm text-orange-600")
                elif t.status == TransactionStatus.REALISEE:
                    if t.auto_side == side:
                        ui.label("✅ Validé automatiquement (vous n'avez pas répondu à temps).").classes(
                            "text-sm text-gray-600"
                        )

                        def make_contest(tid=t.id, s=side, other_email=other.user_email):
                            def _contest():
                                ok, _ = repo.contest(tid, s, CONTEST_GRACE)
                                if ok:
                                    ui.notify("Litige rouvert, un modérateur va examiner.", type="warning")
                                    notif.notify(
                                        [other_email],
                                        "La validation automatique d'un échange vous concernant a été "
                                        "contestée — un modérateur va trancher.",
                                    )
                                    if MODERATEUR_EMAILS:
                                        notif.notify(
                                            MODERATEUR_EMAILS,
                                            f"Litige rouvert par contestation sur échange-billet (transaction {tid}).",
                                        )
                                else:
                                    ui.notify("Fenêtre de contestation dépassée.", type="negative")
                                ui.navigate.to("/mes-echanges")
                            return _contest

                        ui.button("Ce n'est pas arrivé — contester", on_click=make_contest()).props(
                            "flat color=negative"
                        )
                    else:
                        ui.label("✅ Échange réalisé.").classes("text-sm text-green-600")
                elif already_confirmed:
                    ui.label("Vous avez confirmé, en attente de l'autre partie.").classes("text-sm text-green-600")
                else:
                    with ui.row().classes("gap-2 mt-2"):
                        def make_confirm(tid=t.id, s=side):
                            def _confirm():
                                repo.confirm(tid, s)
                                ui.navigate.to("/mes-echanges")
                            return _confirm

                        def make_dispute(tid=t.id, s=side, other_email=other.user_email):
                            def _dispute():
                                repo.dispute(tid, s)
                                notif.notify(
                                    [other_email],
                                    "L'autre partie indique que votre échange ne s'est pas déroulé comme "
                                    "prévu — un modérateur va examiner la situation.",
                                )
                                if MODERATEUR_EMAILS:
                                    notif.notify(
                                        MODERATEUR_EMAILS,
                                        f"Nouveau litige à trancher sur échange-billet (transaction {tid}).",
                                    )
                                ui.navigate.to("/mes-echanges")
                            return _dispute

                        ui.button("J'ai réalisé l'échange", on_click=make_confirm()).props("color=primary")
                        ui.button("Ça ne s'est pas passé", on_click=make_dispute()).props("flat color=negative")


@ui.page("/moderation")
def moderation():
    user_info = appacl.get_user_info(app)
    if not user_info or not require_tier(user_info, "moderateur"):
        access_denied(ROLE_MODERATEUR)
        return

    with ui.column().classes("w-full max-w-3xl mx-auto gap-4 p-4"):
        ui.label("Modération").classes("text-xl font-bold")
        ui.button("← Retour", on_click=lambda: ui.navigate.to("/")).props("flat")

        ui.label("Créneaux en attente de validation").classes("text-lg font-semibold mt-2")
        pending = repo.list_slots(SHOW_ID, SlotStatus.EN_ATTENTE)
        if not pending:
            ui.label("Aucun créneau en attente.").classes("text-gray-500")
        for slot, _, _ in pending:
            with ui.card().classes("w-full"):
                ui.label(str(slot))
                with ui.row().classes("gap-2"):
                    def make_validate(s=slot):
                        def _validate():
                            repo.set_slot_status(SHOW_ID, s, SlotStatus.VALIDE)
                            ui.navigate.to("/moderation")
                        return _validate

                    def make_reject(s=slot):
                        def _reject():
                            repo.set_slot_status(SHOW_ID, s, SlotStatus.REJETE)
                            ui.navigate.to("/moderation")
                        return _reject

                    ui.button("Valider", on_click=make_validate()).props("color=primary")
                    ui.button("Rejeter", on_click=make_reject()).props("flat color=negative")

        litiges = repo.litiges(SHOW_ID)
        ui.label("Litiges à trancher").classes("text-lg font-semibold mt-4")
        if not litiges:
            ui.label("Aucun litige en cours.").classes("text-gray-500")
        for t in litiges:
            la, lb = repo.get_listing(t.listing_a_id), repo.get_listing(t.listing_b_id)
            with ui.card().classes("w-full"):
                ui.label(f"{t.count} place(s) — {t.slot}")
                ui.label(f"{la.user_name} <{la.user_email}>  ↔  {lb.user_name} <{lb.user_email}>").classes(
                    "text-sm text-gray-500"
                )
                details = []
                if t.disputed_a_at:
                    details.append(f"{la.user_name} a signalé un problème")
                if t.disputed_b_at:
                    details.append(f"{lb.user_name} a signalé un problème")
                if t.auto_side:
                    autre = la if t.auto_side == "a" else lb
                    details.append(f"{autre.user_name} conteste une validation automatique")
                if details:
                    ui.label(" · ".join(details)).classes("text-xs text-gray-400")

                with ui.row().classes("gap-2"):
                    def make_resolve(tid=t.id, outcome=TransactionStatus.REALISEE, la=la, lb=lb):
                        def _resolve():
                            ok = repo.resolve_litige(tid, user_info["email"], outcome)
                            if not ok:
                                ui.notify("Ce litige a déjà été tranché entre-temps.", type="warning")
                            else:
                                verdict = "réalisé" if outcome == TransactionStatus.REALISEE else "invalidé"
                                notif.notify(
                                    [la.user_email, lb.user_email],
                                    f"Un modérateur a tranché votre litige : l'échange est considéré {verdict}.",
                                )
                            ui.navigate.to("/moderation")
                        return _resolve

                    ui.button("Confirmer l'échange", on_click=make_resolve(outcome=TransactionStatus.REALISEE)).props(
                        "color=primary"
                    )
                    ui.button("Invalider l'échange", on_click=make_resolve(outcome=TransactionStatus.ANNULEE)).props(
                        "flat color=negative"
                    )


@ui.page("/preferences")
def preferences():
    user_info = appacl.get_user_info(app)
    if not user_info:
        ui.navigate.to("/login")
        return
    if not require_tier(user_info, "utilisateur"):
        access_denied(ROLE_UTILISATEUR)
        return

    valid_slots = [s for s, _, _ in repo.list_slots(SHOW_ID, SlotStatus.VALIDE)]
    slot_options = {s.key(): str(s) for s in valid_slots}

    with ui.column().classes("w-full max-w-3xl mx-auto gap-4 p-4"):
        ui.label("Préférences pour la répartition initiale").classes("text-xl font-bold")
        ui.button("← Retour", on_click=lambda: ui.navigate.to("/")).props("flat")

        existing = repo.get_preference(SHOW_ID, user_info["email"])
        if existing:
            count, tiers, submitted_at = existing
            resume = " > ".join(
                ("n'importe lequel" if not t else "/".join(str(s) for s in t)) for t in tiers
            )
            ui.label(f"Préférence déjà soumise le {submitted_at:%d/%m} : {count} place(s) — {resume}").classes(
                "text-sm text-gray-500 italic"
            )
            ui.label("Soumettre à nouveau ci-dessous remplace cette préférence.").classes("text-xs text-gray-400")

        if not valid_slots:
            ui.label("Aucun créneau n'a encore été défini par un gestionnaire.").classes("text-gray-500")
            return

        count_input = ui.number("Nombre de places souhaitées (0 = je ne veux pas de billet)", value=1, min=0, max=20) \
            .classes("w-full")

        ui.label("Niveaux de préférence, du meilleur au pire — laisser un niveau sans créneau coché = "
                 "« n'importe lequel parmi ceux qui restent » pour ce niveau.")
        tiers_container = ui.column().classes("w-full gap-2")
        tiers_state = []

        def add_tier():
            tier_index = len(tiers_state) + 1
            with tiers_container:
                with ui.card().classes("w-full bg-gray-50"):
                    ui.label(f"Niveau {tier_index}").classes("font-semibold")
                    select = ui.select(slot_options, multiple=True, label="Créneaux acceptés à ce niveau").classes("w-full")
            tiers_state.append(select)

        add_tier()
        ui.button("+ ajouter un niveau de préférence", on_click=add_tier).props("flat")
        feedback = ui.label().classes("text-sm")

        def submit():
            count = int(count_input.value)
            if count == 0:
                repo.submit_preference(SHOW_ID, user_info["email"], user_info.get("name", user_info["email"]), 0, [])
                ui.notify("Préférence enregistrée : vous ne recevrez pas de billet.", type="positive")
                ui.navigate.to("/")
                return
            tiers = [
                [Slot.from_key(k) for k in (select.value or [])]
                for select in tiers_state
            ]  # un niveau sans case cochée reste valide : c'est un joker ([])
            if not tiers:
                feedback.text = "Ajoute au moins un niveau de préférence (même vide = n'importe lequel)."
                return
            repo.submit_preference(
                SHOW_ID, user_info["email"], user_info.get("name", user_info["email"]), count, tiers,
            )
            ui.notify("Préférence enregistrée.", type="positive")
            ui.navigate.to("/")

        ui.button("Enregistrer mes préférences", on_click=submit).props("color=primary")


@ui.page("/historique-tirages")
def historique_tirages():
    """Public (tout utilisateur) : transparence sur les relances du
    tirage au sort — combien, par qui, pourquoi, avec quel résultat
    (même les essais jamais publiés)."""
    user_info = appacl.get_user_info(app)
    if not user_info:
        ui.navigate.to("/login")
        return
    if not require_tier(user_info, "utilisateur"):
        access_denied(ROLE_UTILISATEUR)
        return

    with ui.column().classes("w-full max-w-3xl mx-auto gap-4 p-4"):
        ui.label("Historique des tirages — répartition initiale").classes("text-xl font-bold")
        ui.button("← Retour", on_click=lambda: ui.navigate.to("/")).props("flat")

        publication = repo.get_publication(SHOW_ID)
        historique = list_draws(repo, SHOW_ID)
        if not historique:
            ui.label("Aucun tirage effectué pour le moment.").classes("text-gray-500")

        for d in historique:
            est_publie = publication is not None and publication[4] == d.iteration
            with ui.card().classes("w-full" + (" bg-green-50" if est_publie else "")):
                ui.label(f"Tirage n°{d.iteration}" + (" — PUBLIÉ (définitif)" if est_publie else "")) \
                    .classes("font-semibold")
                ui.label(f"Par {d.created_by}, le {d.created_at:%d/%m/%Y à %Hh%M}").classes("text-sm text-gray-500")
                ui.label(f"Justification : {d.justification}").classes("text-sm")
                ui.label(f"Graine : {d.seed}").classes("text-xs text-gray-400")
                ui.label("Ordre tiré : " + ", ".join(d.draw_order)).classes("text-xs text-gray-400")


@ui.page("/gestion")
def gestion():
    user_info = appacl.get_user_info(app)
    if not user_info or not is_gestionnaire(user_info):
        access_denied(ROLE_GESTIONNAIRE)
        return

    deja_publie = repo.is_published(SHOW_ID)

    with ui.column().classes("w-full max-w-3xl mx-auto gap-4 p-4"):
        ui.label("Gestion — créneaux, billets et répartition").classes("text-xl font-bold")
        ui.button("← Retour", on_click=lambda: ui.navigate.to("/")).props("flat")

        ui.label("Créneaux et capacités").classes("text-lg font-semibold mt-2")
        for slot, status, capacity in repo.list_slots(SHOW_ID):
            with ui.row().classes("w-full items-center gap-2"):
                ui.label(f"{slot} ({status.value})").classes("flex-1")
                cap_input = ui.number(value=capacity or 0, min=0, max=1000).classes("w-24")

                def make_save(s=slot, inp=cap_input):
                    def _save():
                        repo.set_slot_capacity(SHOW_ID, s, int(inp.value), user_info["email"])
                        ui.notify(f"Capacité de {s} mise à jour.", type="positive")
                    return _save

                ui.button("Enregistrer", on_click=make_save()).props("flat dense")

        ui.label("Ajouter un nouveau créneau").classes("font-semibold mt-2")
        with ui.row().classes("w-full gap-2"):
            new_date = ui.date(label="Date").classes("flex-1")
            new_time = ui.time(label="Heure").classes("flex-1")
            new_capacity = ui.number("Billets disponibles", value=0, min=0, max=1000).classes("flex-1")

        def add_slot():
            if not (new_date.value and new_time.value):
                ui.notify("Indique une date et une heure.", type="negative")
                return
            slot = Slot(date.fromisoformat(new_date.value), time.fromisoformat(new_time.value))
            repo.set_slot_capacity(SHOW_ID, slot, int(new_capacity.value), user_info["email"])
            ui.notify(f"Créneau {slot} ajouté.", type="positive")
            ui.navigate.to("/gestion")

        ui.button("Ajouter le créneau", on_click=add_slot).props("color=primary")

        ui.label("Répartition initiale — tirage au sort pondéré").classes("text-lg font-semibold mt-4")
        ui.label(f"{len(repo.list_preferences(SHOW_ID))} préférence(s) soumise(s).").classes("text-sm text-gray-500")
        ui.label(
            "L'aperçu peut être relancé autant de fois que nécessaire pendant la collecte : il simule "
            "le tirage des centaines de fois avec des graines différentes et affiche, pour chaque "
            "personne, la probabilité empirique d'obtenir chacun des créneaux demandés. Le tirage "
            "définitif, lui, n'est tiré qu'UNE SEULE FOIS, avec une graine enregistrée, et ne peut "
            "jamais être rejoué."
        ).classes("text-xs text-gray-500 italic")

        def _participants_ponderes():
            prefs_actuelles = repo.list_preferences(SHOW_ID)
            participants_sans_poids = [
                Participant(email, name, count, tiers)
                for email, name, count, tiers in prefs_actuelles if count > 0
            ]
            penalite = default_unserved_penalty(participants_sans_poids)
            return [
                Participant(
                    p.id, p.name, p.count, p.tiers,
                    priority_weight=compute_priority_weight(repo.get_historique(p.id, SHOW_ID), penalite),
                )
                for p in participants_sans_poids
            ]

        def _supply_actuelle():
            valid = repo.list_slots(SHOW_ID, SlotStatus.VALIDE)
            return [SlotSupply(slot, capacity) for slot, _, capacity in valid if capacity]

        publication = repo.get_publication(SHOW_ID)
        if publication:
            published_by, published_at, seed, draw_order, iteration = publication
            with ui.card().classes("w-full bg-green-50"):
                ui.label(f"Tirage définitif réalisé le {published_at:%d/%m/%Y à %Hh%M} par {published_by} "
                         f"(itération n°{iteration}, soit {iteration - 1} relance(s) avant publication).") \
                    .classes("font-semibold")
                ui.label(f"Graine : {seed}").classes("text-xs text-gray-500")
                ui.label("Ordre tiré : " + ", ".join(draw_order)).classes("text-xs text-gray-500")

        resultats_container = ui.column().classes("w-full gap-2")

        def voir_apercu():
            participants = _participants_ponderes()
            supply = _supply_actuelle()
            if not supply:
                ui.notify("Aucun créneau avec une capacité définie.", type="negative")
                return
            if not participants:
                ui.notify("Aucune préférence avec une demande de places.", type="negative")
                return
            probabilites = preview_probabilities(participants, supply, n_simulations=500)
            resultats_container.clear()
            with resultats_container:
                ui.label("Aperçu (probabilités, recalculable à volonté — n'engage à rien)").classes("font-semibold")
                slot_str = {s.slot.key(): str(s.slot) for s in supply}
                for p in participants:
                    mes_probas = probabilites.get(p.id, {})
                    detail = ", ".join(f"{slot_str.get(k, k)} : {v:.0%}" for k, v in mes_probas.items()) or "aucune chance actuellement"
                    non_servi = 1.0 - sum(mes_probas.values())
                    ui.label(f"{p.name} ({p.count} place(s) demandée(s)) : {detail}" +
                             (f" — non servi : {non_servi:.0%}" if non_servi > 0.01 else ""))

        ui.button("Voir l'aperçu (probabilités)", on_click=voir_apercu).props("color=primary")

        if not deja_publie:
            ui.label(
                "Un tirage peut être relancé autant de fois que nécessaire — mais chaque essai est "
                "enregistré, visible par tous (page « Historique des tirages »), et exige une "
                "justification. Seule la publication finale est irréversible."
            ).classes("text-xs text-gray-500 italic")

            historique_actuel = list_draws(repo, SHOW_ID)
            dernier_tirage_container = ui.column().classes("w-full gap-2")

            def afficher_tirage(d):
                dernier_tirage_container.clear()
                with dernier_tirage_container:
                    with ui.card().classes("w-full bg-blue-50"):
                        ui.label(f"Tirage n°{d.iteration} — graine {d.seed}").classes("font-semibold")
                        ui.label(f"Justification : {d.justification}").classes("text-sm")
                        for a in d.assignments:
                            if a.slot:
                                ui.label(f"{a.participant_name} : {a.count} place(s) — {a.slot} (rang {a.tier_index})")
                            else:
                                ui.label(f"{a.participant_name} : NON SERVI").classes("text-red-600")

                        def publier_ce_tirage():
                            if repo.is_published(SHOW_ID):
                                ui.notify("Déjà publié entre-temps.", type="negative")
                                return
                            for a in d.assignments:
                                if a.slot is not None:
                                    repo.add_listing(new_listing(
                                        show_id=SHOW_ID, type_=ListingType.ECHANGE.value,
                                        user_email=a.participant_id, user_name=a.participant_name,
                                        offered_slot=a.slot, offered_count=a.count, wanted_groups=[],
                                        is_gift=False, note="Attribution initiale (tirage au sort)",
                                    ))
                                repo.record_historique(SHOW_ID, a.participant_id, a.tier_index)
                            repo.mark_published(SHOW_ID, user_info["email"], d.seed, d.draw_order, d.iteration)
                            ui.notify("Tirage publié — définitif.", type="positive")
                            ui.navigate.to("/gestion")

                        with ui.row().classes("gap-2 mt-2"):
                            ui.button("Publier ce tirage (définitif)", on_click=publier_ce_tirage).props(
                                "color=negative"
                            )

            if historique_actuel:
                afficher_tirage(historique_actuel[-1])

            justification_input = ui.textarea(
                "Justification (obligatoire, visible par tous)"
            ).classes("w-full")

            def lancer_tirage():
                participants = _participants_ponderes()
                supply = _supply_actuelle()
                if not supply or not participants:
                    ui.notify("Aucun créneau ou aucune préférence à traiter.", type="negative")
                    return
                try:
                    d = run_draw_iteration(
                        repo, SHOW_ID, participants, supply, justification_input.value, user_info["email"],
                    )
                except (ValueError, RuntimeError) as e:
                    ui.notify(str(e), type="negative")
                    return
                justification_input.value = ""
                afficher_tirage(d)
                ui.notify(f"Tirage n°{d.iteration} réalisé — pas encore publié.", type="positive")

            label_bouton = "Lancer un tirage" if not historique_actuel else "Relancer un tirage"
            ui.button(label_bouton, on_click=lancer_tirage).props("color=primary")

        if MAX_CYCLE_LENGTH_CEILING >= 3:
            ui.label("Cycles d'échange à N parties").classes("text-lg font-semibold mt-6")
            ui.label(
                "Un cycle met en relation plusieurs personnes en chaîne (A donne à B, B à C, ..., Z à A) "
                "sans qu'aucune ne rencontre plus d'une seule autre personne. Recherché et proposé "
                "AUTOMATIQUEMENT par le cycle quotidien (comme le bilatéral) dès que vous relevez la "
                "longueur ci-dessous — vous ne faites que régler ce curseur, jamais valider un échange "
                "vous-même."
            ).classes("text-xs text-gray-500 italic")

            longueur_actuelle = repo.get_max_cycle_length(SHOW_ID, MAX_CYCLE_LENGTH_CEILING)
            with ui.row().classes("items-center gap-2"):
                longueur_input = ui.number(
                    f"Longueur maximale (3 à {MAX_CYCLE_LENGTH_CEILING})",
                    value=max(longueur_actuelle, 3), min=3, max=MAX_CYCLE_LENGTH_CEILING,
                ).classes("w-64")

                def save_longueur():
                    ok, msg = repo.set_max_cycle_length(SHOW_ID, int(longueur_input.value), MAX_CYCLE_LENGTH_CEILING)
                    ui.notify(msg, type="positive" if ok else "negative")

                ui.button("Enregistrer", on_click=save_longueur).props("flat dense")


if __name__ in {"__main__", "__mp_main__"}:
    appacl.set(app, client_id=os.environ.get("OIDC_CLIENT_ID", "echange-billet"))
    ui.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8080)), storage_secret=os.environ["STORAGE_SECRET"])
