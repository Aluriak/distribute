"""STUB — à remplacer par l'implémentation réelle (relais SMTP du CHU).

Sans ce fichier, rien dans échange-billet ne peut s'importer (app.py,
cron.py et models.py appellent tous `notif.notify(...)`). Ce stub logge
simplement sur stdout, pour que le dépôt reste utilisable (tests, démo,
développement local) avant le branchement réel.

Interface attendue par le reste du code — à conserver telle quelle :
    notify(agents: list[str], body: str) -> None
"""
import logging

logger = logging.getLogger("notif")


def notify(agents: list[str], body: str) -> None:
    logger.info("[STUB notif.py] destinataires=%s\n%s", agents, body)
