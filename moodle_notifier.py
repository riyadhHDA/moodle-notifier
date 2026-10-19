#!/usr/bin/env python3
"""moodle-notifier — surveille des cours Moodle et notifie par e-mail.

Utilise l'API Web Services officielle de Moodle (REST).
Aucun scraping HTML, aucun navigateur headless.

Commandes utiles :
    python3 moodle_notifier.py --test-moodle
    python3 moodle_notifier.py --list-courses
    python3 moodle_notifier.py --test-course 12345
    python3 moodle_notifier.py --test-smtp
    python3 moodle_notifier.py --once [--dry-run]
    python3 moodle_notifier.py                # boucle de surveillance (service)
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import requests

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover
    LOCAL_TZ = timezone.utc

DEFAULT_ENV_FILE = "/etc/moodle-notifier/moodle-notifier.env"
DEFAULT_COURSES_FILE = "/etc/moodle-notifier/courses.yml"
DEFAULT_STATE_FILE = "/var/lib/moodle-notifier/state.json"
STATE_VERSION = 1
API_SERVICE = "moodle_mobile_app"
USER_AGENT = "moodle-notifier/1.0"

log = logging.getLogger("moodle-notifier")

MODNAME_LABELS = {
    "resource": "Ressource",
    "folder": "Dossier",
    "url": "Lien web",
    "page": "Page",
    "book": "Livre",
    "file": "Fichier",
    "forum": "Forum",
    "chat": "Chat",
    "assign": "Devoir",
    "quiz": "Test / Quiz",
    "choice": "Sondage",
    "feedback": "Feedback",
    "lesson": "Leçon",
    "scorm": "SCORM",
    "lti": "Outil externe",
    "label": "Étiquette",
    "workshop": "Atelier",
    "wiki": "Wiki",
    "glossary": "Glossaire",
    "data": "Base de données",
    "h5pactivity": "Activité H5P",
    "imscp": "Paquetage IMS",
    "bigbluebuttonbn": "Classe virtuelle",
    "subsection": "Sous-section",
}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def describe_exc(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def load_env_file(path: Path) -> None:
    """Charge un fichier KEY=VALUE sans écraser les variables déjà définies."""
    if not path.is_file():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("Fichier d'environnement illisible (%s) : %s", path, exc)
        return
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Config:
    moodle_url: str
    moodle_token: str
    moodle_username: str
    moodle_password: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    smtp_password: str
    mail_from: str
    mail_to: list
    check_interval: int
    timeout: int
    state_file: Path
    courses_file: Path
    dry_run: bool = False

    @classmethod
    def from_env(cls, args) -> "Config":
        def env(name: str, default: str = "") -> str:
            return (os.environ.get(name) or default).strip()

        state = args.state_file or env("STATE_FILE", DEFAULT_STATE_FILE)
        courses = args.courses_file or env("COURSES_FILE", DEFAULT_COURSES_FILE)
        try:
            interval = int(args.interval if args.interval is not None else env("CHECK_INTERVAL", "120") or 120)
            port = int(env("SMTP_PORT", "587") or 587)
            timeout = int(env("HTTP_TIMEOUT", "30") or 30)
        except ValueError as exc:
            raise SystemExit(f"Valeur numérique invalide dans la configuration : {exc}")

        return cls(
            moodle_url=env("MOODLE_URL", "https://moodle.unistra.fr").rstrip("/"),
            moodle_token=env("MOODLE_TOKEN"),
            moodle_username=env("MOODLE_USERNAME"),
            moodle_password=os.environ.get("MOODLE_PASSWORD", ""),
            smtp_host=env("SMTP_HOST"),
            smtp_port=port,
            smtp_security=env("SMTP_SECURITY", "starttls").lower(),
            smtp_username=env("SMTP_USERNAME"),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            mail_from=env("MAIL_FROM") or env("SMTP_USERNAME"),
            mail_to=[x.strip() for x in env("MAIL_TO").split(",") if x.strip()],
            check_interval=max(10, interval),
            timeout=max(5, timeout),
            state_file=Path(state),
            courses_file=Path(courses),
            dry_run=bool(args.dry_run),
        )


def load_courses_config(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"Fichier de configuration des cours introuvable : {path}")
    text = path.read_text(encoding="utf-8")
    data = None
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
        except Exception as exc:
            raise SystemExit(f"YAML invalide ({path}) : {exc}")
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"JSON invalide ({path}) et module PyYAML absent : {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"Format invalide ({path}) : attendu un dictionnaire avec une clé « courses ».")
    return data


# --------------------------------------------------------------------------- #
# Client API Moodle
# --------------------------------------------------------------------------- #

class MoodleAPIError(RuntimeError):
    def __init__(self, errorcode: str, message: str):
        super().__init__(f"{errorcode}: {message}")
        self.errorcode = errorcode


class MoodleTokenExpired(MoodleAPIError):
    pass


class MoodleClient:
    EXPIRED_CODES = {"invalidtoken", "accessexception"}

    def __init__(self, base_url: str, token=None, username=None, password=None, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = (token or "").strip() or None
        self.username = username or None
        self.password = password or None
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def login(self) -> None:
        if not (self.username and self.password):
            raise MoodleAPIError(
                "noauth",
                "aucun identifiant Moodle (définir MOODLE_TOKEN, ou MOODLE_USERNAME + MOODLE_PASSWORD)",
            )
        url = f"{self.base_url}/login/token.php"
        try:
            resp = self.session.post(
                url,
                data={"username": self.username, "password": self.password, "service": API_SERVICE},
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise MoodleAPIError("network", describe_exc(exc))
        try:
            payload = resp.json()
        except ValueError:
            raise MoodleAPIError("invalidresponse", "réponse non-JSON de login/token.php")
        token = payload.get("token")
        if not token:
            raise MoodleAPIError(payload.get("errorcode") or "autherror", payload.get("error") or "authentification refusée")
        self.token = token
        log.info("Authentification Moodle réussie (jeton obtenu).")

    def _api(self, function: str, params: dict | None = None):
        if not self.token:
            self.login()
        url = f"{self.base_url}/webservice/rest/server.php"
        data = {"wstoken": self.token, "wsfunction": function, "moodlewsrestformat": "json"}
        if params:
            data.update(params)
        try:
            resp = self.session.post(url, data=data, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise MoodleAPIError("network", describe_exc(exc))
        try:
            payload = resp.json()
        except ValueError:
            raise MoodleAPIError("invalidresponse", f"réponse non-JSON pour {function}")
        if isinstance(payload, dict) and payload.get("exception"):
            code = payload.get("errorcode") or "error"
            message = payload.get("message") or "erreur API"
            if code in self.EXPIRED_CODES:
                self.token = None
                raise MoodleTokenExpired(code, message)
            raise MoodleAPIError(code, message)
        return payload

    # --- fonctions API utilisées ------------------------------------------ #

    def get_site_info(self) -> dict:
        return self._api("core_webservice_get_site_info")

    def get_users_courses(self, userid: int) -> list:
        return self._api("core_enrol_get_users_courses", {"userid": userid})

    def get_course_contents(self, courseid: int) -> list:
        return self._api("core_course_get_contents", {"courseid": courseid})


# --------------------------------------------------------------------------- #
# Résolution des cours surveillés
# --------------------------------------------------------------------------- #

def normalize(value) -> str:
    return " ".join(str(value or "").casefold().split())


def resolve_courses(client: MoodleClient, conf: dict, cfg: Config) -> list:
    info = client.get_site_info()
    userid = int(info["userid"])
    all_courses = client.get_users_courses(userid)
    entries = conf.get("courses")
    if not entries or entries == "all" or conf.get("all") is True:
        return [
            {"id": int(c["id"]), "name": c.get("fullname") or c.get("shortname") or str(c["id"])}
            for c in all_courses
        ]
    if not isinstance(entries, list):
        raise SystemExit("Le fichier des cours doit contenir une liste « courses » (ou toutes les surveiller avec « courses: all »).")
    resolved, used = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cid, cname = entry.get("id"), entry.get("name")
        match = None
        if cid is not None:
            match = next((c for c in all_courses if int(c["id"]) == int(cid)), None)
            if match is None:
                log.error("Cours id=%s introuvable ou non accessible.", cid)
                continue
        else:
            target = normalize(cname)
            exact = [c for c in all_courses if normalize(c.get("fullname")) == target or normalize(c.get("shortname")) == target]
            if not exact and target:
                exact = [c for c in all_courses if target in normalize(c.get("fullname"))]
            if not exact:
                log.error("Aucun cours ne correspond à « %s » (voir --list-courses).", cname)
                continue
            if len(exact) > 1:
                log.warning("« %s » correspond à %d cours ; le premier est utilisé : %s", cname, len(exact), exact[0].get("fullname"))
            match = exact[0]
        cid_int = int(match["id"])
        if cid_int in used:
            continue
        used.add(cid_int)
        resolved.append({"id": cid_int, "name": cname or match.get("fullname") or str(cid_int)})
    if not resolved:
        log.error("Aucun cours résolu ; vérifie le fichier %s.", cfg.courses_file)
    return resolved


# --------------------------------------------------------------------------- #
# Extraction et comparaison des contenus
# --------------------------------------------------------------------------- #

def extract_items(sections: list) -> dict:
    items = {}
    for section in sections or []:
        section_name = (section.get("name") or "").strip()
        for module in section.get("modules") or []:
            if module.get("uservisible") is False:
                continue
            try:
                cmid = int(module.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if not cmid:
                continue
            files = []
            for content in module.get("contents") or []:
                if content.get("type") != "file":
                    continue
                files.append(
                    {
                        "filename": content.get("filename") or "",
                        "timemodified": int(content.get("timemodified") or 0),
                        "filesize": int(content.get("filesize") or 0),
                    }
                )
            files.sort(key=lambda f: (f["filename"], f["timemodified"]))
            payload = {
                "name": module.get("name") or "",
                "modname": module.get("modname") or "",
                "url": module.get("url") or "",
                "section": section_name,
                "files": files,
                "timemodified": max([f["timemodified"] for f in files] or [0]),
            }
            payload["fingerprint"] = hashlib.sha1(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            items[str(cmid)] = payload
    return items


def diff_course(previous: dict, current: dict):
    added = [(cmid, item) for cmid, item in current.items() if cmid not in previous]
    changed = [
        (cmid, item)
        for cmid, item in current.items()
        if cmid in previous and previous[cmid].get("fingerprint") != item.get("fingerprint")
    ]
    removed = [cmid for cmid in previous if cmid not in current]
    return added, changed, removed


def collect_events(cfg: Config, client: MoodleClient, courses: list, state: dict):
    events = []
    dirty = False
    for course in courses:
        cid = str(course["id"])
        name = course["name"]
        try:
            sections = client.get_course_contents(course["id"])
        except MoodleTokenExpired:
            raise
        except (MoodleAPIError, requests.RequestException) as exc:
            log.error("« %s » : récupération impossible (%s). Nouvelle tentative au prochain cycle.", name, describe_exc(exc))
            continue
        current = extract_items(sections)
        stored = state["courses"].get(cid)
        if stored is None or not stored.get("initialized"):
            state["courses"][cid] = {"name": name, "initialized": True, "items": current}
            log.info("« %s » : initialisation silencieuse (%d élément(s)).", name, len(current))
            dirty = True
            continue
        stored["name"] = name
        previous = stored.get("items") or {}
        added, changed, removed = diff_course(previous, current)
        if added or changed or removed:
            stored["items"] = current
            dirty = True
        if added:
            log.info("« %s » : %d nouvel/ nouveaux élément(s).", name, len(added))
        if changed:
            log.info("« %s » : %d élément(s) modifié(s).", name, len(changed))
        if removed:
            log.info("« %s » : %d élément(s) retiré(s) (mise à jour silencieuse).", name, len(removed))
        for kind, pairs in (("added", added), ("changed", changed)):
            for cmid, item in pairs:
                events.append({"course": course, "cmid": cmid, "item": item, "kind": kind})
    return events, dirty


# --------------------------------------------------------------------------- #
# État local
# --------------------------------------------------------------------------- #

def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"version": STATE_VERSION, "courses": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("État illisible (%s) : %s — un nouvel état sera créé.", path, describe_exc(exc))
        return {"version": STATE_VERSION, "courses": {}}
    if not isinstance(data, dict) or not isinstance(data.get("courses"), dict):
        return {"version": STATE_VERSION, "courses": {}}
    data.setdefault("version", STATE_VERSION)
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# E-mails
# --------------------------------------------------------------------------- #

def fmt_ts(timestamp) -> str:
    try:
        ts = int(timestamp or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0:
        return "date inconnue"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")


def item_type(item: dict) -> str:
    modname = item.get("modname") or ""
    label = MODNAME_LABELS.get(modname, modname or "Élément")
    files = item.get("files") or []
    if len(files) == 1:
        filename = files[0].get("filename") or ""
        if "." in filename:
            ext = filename.rsplit(".", 1)[-1].upper()
            if 1 <= len(ext) <= 6:
                return f"{label} ({ext})"
    elif len(files) > 1:
        return f"{label} ({len(files)} fichiers)"
    return label


def build_email(cfg: Config, events: list):
    grouped: dict = {}
    for event in events:
        cid = str(event["course"]["id"])
        group = grouped.setdefault(cid, {"id": cid, "name": event["course"]["name"], "events": []})
        group["events"].append(event)

    any_added = any(event["kind"] == "added" for event in events)
    headline = "Nouveau contenu détecté" if any_added else "Contenu modifié"
    names = [group["name"] for group in grouped.values()]
    shown = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
    subject = f"[Moodle] {headline} — {shown}"[:180]

    text_lines = [f"{headline} sur {cfg.moodle_url}", ""]
    html_parts = [f'<p>{html.escape(headline)} sur <a href="{html.escape(cfg.moodle_url, quote=True)}">{html.escape(cfg.moodle_url)}</a></p>']

    for group in grouped.values():
        text_lines.append(f"== {group['name']} ==")
        html_parts.append(f"<h3>{html.escape(group['name'])}</h3><ul>")
        for event in group["events"][:100]:
            item = event["item"]
            label = "Ajout" if event["kind"] == "added" else "Modification"
            url = item.get("url") or f"{cfg.moodle_url}/course/view.php?id={group['id']}"
            type_label = item_type(item)
            date_label = fmt_ts(item.get("timemodified"))
            text_lines.append(f"[{label}] {item.get('name') or 'Sans titre'} — {type_label}")
            text_lines.append(f"  Ajout/modification : {date_label}")
            text_lines.append(f"  {url}")
            text_lines.append("")
            html_parts.append(
                f'<li><b>[{label}]</b> <a href="{html.escape(url, quote=True)}">{html.escape(item.get("name") or "Sans titre")}</a>'
                f" — {html.escape(type_label)}<br><small>Ajout/modification : {html.escape(date_label)}</small></li>"
            )
        if len(group["events"]) > 100:
            extra = len(group["events"]) - 100
            text_lines.append(f"(+ {extra} autre(s) élément(s) non listé(s))")
            html_parts.append(f"<li>(+ {extra} autre(s) élément(s) non listé(s))</li>")
        html_parts.append("</ul>")

    text_lines.append("-- ")
    text_lines.append("moodle-notifier")
    return subject, "\n".join(text_lines), "".join(html_parts)


def smtp_connect(cfg: Config):
    if not cfg.smtp_host:
        raise RuntimeError("SMTP_HOST non configuré")
    context = ssl.create_default_context()
    if cfg.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
        server.ehlo()
        if cfg.smtp_security == "starttls":
            server.starttls(context=context)
            server.ehlo()
    if cfg.smtp_username:
        server.login(cfg.smtp_username, cfg.smtp_password)
    return server


def send_email(cfg: Config, subject: str, text_body: str, html_body: str | None = None) -> None:
    if not cfg.mail_to:
        raise RuntimeError("MAIL_TO non configuré")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg.mail_from or cfg.smtp_username or "moodle-notifier"
    message["To"] = ", ".join(cfg.mail_to)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    server = smtp_connect(cfg)
    try:
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Cycle de surveillance
# --------------------------------------------------------------------------- #

def run_cycle(cfg: Config, client: MoodleClient, courses: list) -> int:
    state = load_state(cfg.state_file)
    try:
        events, dirty = collect_events(cfg, client, courses, state)
    except MoodleTokenExpired as exc:
        if not (client.username and client.password):
            log.error(
                "Jeton Moodle invalide ou expiré (%s). Régénère un jeton API (procédure dans le README), "
                "mets à jour MOODLE_TOKEN dans /etc/moodle-notifier/moodle-notifier.env, "
                "puis redémarre le service.",
                exc,
            )
            return 1
        log.warning("Jeton Moodle expiré : nouvelle authentification.")
        client.login()
        state = load_state(cfg.state_file)
        events, dirty = collect_events(cfg, client, courses, state)

    if not events:
        log.info("Aucun nouveau contenu (%d cours surveillé(s)).", len(courses))
        if dirty and not cfg.dry_run:
            save_state(cfg.state_file, state)
        return 0

    if cfg.dry_run:
        log.info("[dry-run] %d notification(s) seraient envoyées.", len(events))
        for event in events[:50]:
            log.info(
                "[dry-run] %s | %s | %s | %s",
                event["course"]["name"],
                event["kind"],
                event["item"].get("name"),
                event["item"].get("modname"),
            )
        return 0

    subject, text_body, html_body = build_email(cfg, events)
    try:
        send_email(cfg, subject, text_body, html_body)
    except Exception as exc:
        log.error("Échec d'envoi de l'e-mail (%s) ; état conservé, nouvelle tentative au prochain cycle.", describe_exc(exc))
        return 1
    save_state(cfg.state_file, state)
    log.info("E-mail envoyé : %d élément(s) sur %d cours.", len(events), len({str(e["course"]["id"]) for e in events}))
    return 0


def main_loop(cfg: Config, client: MoodleClient) -> None:
    log.info("Démarrage de la surveillance — intervalle %d s.", cfg.check_interval)
    courses: list = []
    courses_mtime = None
    while True:
        started = time.monotonic()
        try:
            try:
                mtime = cfg.courses_file.stat().st_mtime if cfg.courses_file.is_file() else None
            except OSError:
                mtime = None
            if courses and mtime != courses_mtime:
                log.info("Fichier des cours modifié : rechargement.")
                courses = []
            if not courses:
                conf = load_courses_config(cfg.courses_file)
                courses = resolve_courses(client, conf, cfg)
                courses_mtime = mtime
                if courses:
                    log.info("Cours surveillés : %s", ", ".join(f"{c['name']} [{c['id']}]" for c in courses))
            if courses:
                run_cycle(cfg, client, courses)
        except KeyboardInterrupt:
            log.info("Arrêt demandé.")
            return
        except Exception as exc:
            log.error("Erreur pendant le cycle : %s", describe_exc(exc))
        elapsed = time.monotonic() - started
        delay = max(5.0, cfg.check_interval - elapsed)
        log.debug("Prochain cycle dans %.0f s.", delay)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            log.info("Arrêt demandé.")
            return


# --------------------------------------------------------------------------- #
# Commandes de test
# --------------------------------------------------------------------------- #

def test_smtp(cfg: Config, send: bool = False) -> None:
    if not cfg.smtp_host:
        raise SystemExit("SMTP_HOST non configuré.")
    server = smtp_connect(cfg)
    try:
        server.noop()
        log.info("SMTP OK : connexion et authentification réussies (%s:%s, %s).", cfg.smtp_host, cfg.smtp_port, cfg.smtp_security)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    if send:
        send_email(
            cfg,
            "[Moodle] Test de configuration",
            "Ceci est un e-mail de test envoyé par moodle-notifier.\nSi tu le reçois, la configuration SMTP est correcte.\n",
            "<p>Ceci est un e-mail de test envoyé par <b>moodle-notifier</b>.</p>",
        )
        log.info("E-mail de test envoyé à %s.", ", ".join(cfg.mail_to))


def cmd_moodle(cfg: Config, client: MoodleClient, args) -> int:
    info = client.get_site_info()
    log.info("Moodle : %s (version %s)", info.get("sitename"), info.get("release"))
    log.info("Utilisateur : %s (id %s)", info.get("fullname") or info.get("username"), info.get("userid"))
    if args.list_courses or args.test_course:
        courses = client.get_users_courses(int(info["userid"]))
        if args.list_courses:
            log.info("%d cours accessibles :", len(courses))
            for course in sorted(courses, key=lambda c: (c.get("fullname") or "").casefold()):
                log.info("  id=%-8s %s", course.get("id"), course.get("fullname"))
    if args.test_course:
        sections = client.get_course_contents(args.test_course)
        items = extract_items(sections)
        log.info("Cours %s : %d section(s), %d élément(s).", args.test_course, len(sections), len(items))
        for cmid, item in list(items.items())[:25]:
            log.info("  cmid=%-8s %-14s %s", cmid, item.get("modname"), item.get("name"))
    return 0


# --------------------------------------------------------------------------- #
# Entrée principale
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--env-file", help=f"fichier de variables (défaut : {DEFAULT_ENV_FILE})")
    parser.add_argument("--courses-file", help="fichier YAML listant les cours à surveiller")
    parser.add_argument("--state-file", help="fichier d'état JSON")
    parser.add_argument("--interval", type=int, help="intervalle entre deux vérifications, en secondes")
    parser.add_argument("--once", action="store_true", help="exécuter un seul cycle puis quitter")
    parser.add_argument("--dry-run", action="store_true", help="ne rien envoyer et ne pas modifier l'état")
    parser.add_argument("--test-moodle", action="store_true", help="tester l'authentification Moodle et afficher les infos du site")
    parser.add_argument("--list-courses", action="store_true", help="lister les cours accessibles (id + nom)")
    parser.add_argument("--test-course", type=int, metavar="ID", help="tester la récupération d'un cours")
    parser.add_argument("--test-smtp", action="store_true", help="tester la connexion SMTP (sans envoyer d'e-mail)")
    parser.add_argument("--send-test-email", action="store_true", help="avec --test-smtp : envoyer un vrai e-mail de test")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    env_path = Path(args.env_file) if args.env_file else Path(os.environ.get("MOODLE_ENV_FILE", DEFAULT_ENV_FILE))
    load_env_file(env_path)
    cfg = Config.from_env(args)

    if args.test_smtp:
        try:
            test_smtp(cfg, send=args.send_test_email)
        except Exception as exc:
            log.error("Échec SMTP : %s", describe_exc(exc))
            return 1
        return 0

    client = MoodleClient(cfg.moodle_url, cfg.moodle_token, cfg.moodle_username, cfg.moodle_password, cfg.timeout)

    if args.test_moodle or args.list_courses or args.test_course:
        try:
            return cmd_moodle(cfg, client, args)
        except Exception as exc:
            log.error("Échec : %s", describe_exc(exc))
            return 1

    if not args.once:
        main_loop(cfg, client)
        return 0

    try:
        conf = load_courses_config(cfg.courses_file)
        courses = resolve_courses(client, conf, cfg)
        if not courses:
            log.error("Aucun cours résolu ; rien à faire.")
            return 1
        return run_cycle(cfg, client, courses)
    except Exception as exc:
        log.error("Échec : %s", describe_exc(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
