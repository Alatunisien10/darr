#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crous_watch.py — Surveillance des logements CROUS et alerte (Telegram + email).

Interroge la recherche de logement CROUS pour une ou plusieurs villes, détecte
les NOUVELLES annonces (par rapport au dernier passage) et notifie dès qu'un
logement apparaît.

Villes préconfigurées : Dunkerque et Béthune (rentrée 2026-2027).

Fonctionne en local (boucle continue) OU hébergé sur GitHub Actions
(un passage --once relancé par le cron, l'état étant commité dans le dépôt).

Dépendances :
    pip install requests beautifulsoup4

Lancement local :
    python crous_watch.py            # boucle (toutes les 5 min par défaut)
    python crous_watch.py --once     # un seul passage (mode GitHub Actions)
    python crous_watch.py --test     # envoie une notif de test puis sort

Notifications : renseigner les variables d'environnement.
  - Telegram : TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
  - Email    : SMTP_USER, SMTP_PASS, MAIL_TO
Chaque canal configuré reçoit l'alerte ; sans configuration, affichage console.
"""

import argparse
import json
import os
import smtplib
import sys
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formataddr

import requests
from bs4 import BeautifulSoup

# ======================================================================
# CONFIGURATION
# ======================================================================

SEARCHES = {
    "Dunkerque": "https://trouverunlogement.lescrous.fr/tools/47/search?bounds=2.305747191702502_51.079736809716735_2.4487578082974983_50.98980479028326&locationName=Dunkerque+%2859140%29",
    "Béthune":   "https://trouverunlogement.lescrous.fr/tools/47/search?bounds=2.6158272_50.5506425_2.6717096_50.5092876&locationName=B%C3%A9thune+%2862400%29",
}

INTERVAL_MINUTES = 5          # fréquence en mode boucle locale
MAX_PAGES = 10                # pages de résultats max par ville
STATE_FILE = "crous_state.json"

# --- Telegram ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Email (optionnel) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")         # ex : ton.adresse@gmail.com
SMTP_PASS = os.environ.get("SMTP_PASS", "")         # mot de passe d'application
MAIL_FROM_NAME = "Alerte Logement CROUS"
MAIL_TO = os.environ.get("MAIL_TO", "")             # à qui envoyer l'alerte

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CrousWatch/1.0; recherche logement etudiant)",
    "Accept-Language": "fr-FR,fr;q=0.9",
}
HTTP_TIMEOUT = 20


# ======================================================================
# LOGIQUE
# ======================================================================

def log(message: str) -> None:
    horodatage = datetime.now().strftime("%d/%m %H:%M:%S")
    print(f"[{horodatage}] {message}", flush=True)


def charger_etat() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log("État illisible, on repart de zéro.")
        return {}


def sauver_etat(etat: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(etat, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        log(f"Impossible d'écrire l'état : {exc}")


def url_page(url_base: str, page: int) -> str:
    separateur = "&" if "?" in url_base else "?"
    return f"{url_base}{separateur}page={page}"


def extraire_logements(html: str, url_base: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    resultats = []
    vus = set()

    cartes = soup.find_all(
        lambda tag: tag.name == "div"
        and tag.get("class")
        and any("fr-card" in c for c in tag.get("class"))
    )

    for carte in cartes:
        lien_tag = None
        titre_tag = carte.find(
            lambda t: t.get("class") and any("fr-card__title" in c for c in t.get("class"))
        )
        if titre_tag:
            lien_tag = titre_tag.find("a")
        if lien_tag is None:
            lien_tag = carte.find("a")
        if lien_tag is None:
            continue

        titre = lien_tag.get_text(strip=True)
        if not titre:
            continue

        href = lien_tag.get("href", "")
        if href.startswith("/"):
            lien = "https://trouverunlogement.lescrous.fr" + href
        elif href.startswith("http"):
            lien = href
        else:
            lien = url_base

        identifiant = href or titre
        if identifiant in vus:
            continue
        vus.add(identifiant)
        resultats.append({"id": identifiant, "titre": titre, "lien": lien})

    return resultats


def rechercher_ville(ville: str, url_base: str) -> list:
    tous = []
    ids_vus = set()

    for page in range(1, MAX_PAGES + 1):
        url = url_page(url_base, page)
        try:
            reponse = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            log(f"{ville} : erreur réseau ({exc}).")
            break

        if reponse.status_code != 200:
            log(f"{ville} : réponse HTTP {reponse.status_code}.")
            break

        logements = extraire_logements(reponse.text, url_base)
        if not logements:
            break

        nouveaux_sur_page = [lg for lg in logements if lg["id"] not in ids_vus]
        if not nouveaux_sur_page:
            break

        for lg in nouveaux_sur_page:
            ids_vus.add(lg["id"])
            tous.append(lg)

        time.sleep(1)

    return tous


def envoyer_telegram(texte: str) -> bool:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return False
    if len(texte) > 4000:
        texte = texte[:3900] + "\n… (liste tronquée, va voir sur le site)"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": texte,
                  "disable_web_page_preview": True},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            log("Notification Telegram envoyée.")
            return True
        log(f"Telegram : réponse HTTP {r.status_code} — {r.text[:200]}")
        return False
    except requests.RequestException as exc:
        log(f"Échec Telegram : {exc}")
        return False


def envoyer_mail(sujet: str, corps: str) -> bool:
    if not (SMTP_USER and SMTP_PASS and MAIL_TO):
        return False
    message = MIMEText(corps, "plain", "utf-8")
    message["Subject"] = sujet
    message["From"] = formataddr((MAIL_FROM_NAME, SMTP_USER))
    message["To"] = MAIL_TO
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=HTTP_TIMEOUT) as serveur:
            serveur.starttls()
            serveur.login(SMTP_USER, SMTP_PASS)
            serveur.sendmail(SMTP_USER, [MAIL_TO], message.as_string())
        log(f"Email envoyé à {MAIL_TO}.")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"Échec de l'envoi de l'email : {exc}")
        return False


def notifier(nouveaux_par_ville: dict) -> None:
    lignes = ["De nouveaux logements CROUS sont disponibles :", ""]
    total = 0
    for ville, logements in nouveaux_par_ville.items():
        lignes.append(f"=== {ville} ({len(logements)}) ===")
        for lg in logements:
            lignes.append(f"- {lg['titre']}")
            lignes.append(f"  {lg['lien']}")
            total += 1
        lignes.append("")
    lignes.append("Va vite réserver, ça part très vite !")

    corps = "\n".join(lignes)
    sujet = f"[CROUS] {total} nouveau(x) logement(s) disponible(s)"
    print(corps)

    telegram_ok = envoyer_telegram(corps)
    mail_ok = envoyer_mail(sujet, corps)
    envoye = telegram_ok or mail_ok
    
    if not envoye:
        log("Aucun canal de notification configuré : affichage console uniquement.")


def un_passage(etat: dict) -> dict:
    nouveaux_par_ville = {}

    for ville, url_base in SEARCHES.items():
        logements = rechercher_ville(ville, url_base)
        deja_vus = set(etat.get(ville, []))
        nouveaux = [lg for lg in logements if lg["id"] not in deja_vus]

        if nouveaux:
            log(f"{ville} : {len(nouveaux)} nouveau(x) logement(s) !")
            nouveaux_par_ville[ville] = nouveaux
        else:
            log(f"{ville} : {len(logements)} logement(s) au total, rien de nouveau.")

        # Correction de la perte de mémoire : on garde l'historique ET les nouveaux
        nouvel_historique = deja_vus.union(lg["id"] for lg in nouveaux)
        etat[ville] = list(nouvel_historique)

    if nouveaux_par_ville:
        notifier(nouveaux_par_ville)

    return etat


def main() -> None:
    parser = argparse.ArgumentParser(description="Surveillance logements CROUS.")
    parser.add_argument("--once", action="store_true",
                        help="Un seul passage puis sortie (mode GitHub Actions).")
    parser.add_argument("--test", action="store_true",
                        help="Envoie une notification de test puis sortie.")
    args = parser.parse_args()

    if args.test:
        notifier({"Test": [{"titre": "Notification de test",
                            "lien": "https://trouverunlogement.lescrous.fr/"}]})
        sys.exit(0)

    log("Démarrage de la surveillance CROUS.")
    log(f"Villes surveillées : {', '.join(SEARCHES.keys())}.")

    etat = charger_etat()

    if args.once:
        etat = un_passage(etat)
        sauver_etat(etat)
        return

    log(f"Vérification toutes les {INTERVAL_MINUTES} minutes. Ctrl+C pour arrêter.")
    try:
        while True:
            etat = un_passage(etat)
            sauver_etat(etat)
            time.sleep(INTERVAL_MINUTES * 60)
    except KeyboardInterrupt:
        log("Arrêt demandé. À bientôt.")


if __name__ == "__main__":
    main()
