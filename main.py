"""
Backend di MiczyAI (Flask) - v2: account, chat salvate, ban e limiti.

Tutte le impostazioni arrivano dalle variabili d'ambiente di Render, MAI dal codice.

  Obbligatorie
    OPENROUTER_API_KEY      chiave di OpenRouter

  Account (se mancano, il sito funziona solo per ospiti, come prima)
    SUPABASE_URL            es. https://abcdefgh.supabase.co
    SUPABASE_ANON_KEY       chiave "publishable"/anon (quella pubblica)
    SUPABASE_SERVICE_KEY    chiave "secret"/service_role  -> SEGRETA, solo qui

  Facoltative
    DISCORD_WEBHOOK_URL     se vuota, nessun messaggio va su Discord
    ALLOWED_ORIGINS         siti autorizzati, separati da virgola
    MODEL                   default: gpt-4o-mini
    MAX_TOKENS              lunghezza massima della risposta, default 800
    RATE_PER_MINUTE         richieste al minuto (per IP o per utente), default 8
    GUEST_PER_DAY           messaggi al giorno per ospite (per IP), default 20
    USER_PER_DAY            messaggi al giorno per utente registrato, default 100
    SYSTEM_PROMPT           istruzioni fisse per l'IA
    DISCORD_ADMIN_ALERTS    "0" per non ricevere su Discord gli avvisi (nuovo utente, sospensioni), default 1
    PRICE_IN_PER_M          costo in dollari per milione di token in ingresso (stima costi), default 0.15
    PRICE_OUT_PER_M         costo in dollari per milione di token in uscita, default 0.60

  Streaming, pollice su/giù, link di condivisione e cartelle richiedono migrazione-chat.sql.

  Dal pannello admin (salvate nel database, non servono variabili): modalità manutenzione,
  avviso in cima al sito, limiti di messaggi, personalità del bot. Richiedono migrazione-admin.sql.
"""

import copy
import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, Response, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from zoneinfo import ZoneInfo
    FUSO = ZoneInfo("Europe/Rome")
except Exception:  # tzdata mancante: si mostra l'ora UTC
    FUSO = timezone.utc

app = Flask(__name__)
# Render sta dietro un proxy: così request.remote_addr è l'IP reale del visitatore
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # corpo della richiesta: max 64 KB

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
# "1" (predefinito): ogni domanda viene inoltrata a Discord in automatico, come prima.
# "0": non si inoltra più nulla da sé; arriva su Discord solo ciò che l'utente invia col pulsante.
DISCORD_AUTO_FORWARD = os.environ.get("DISCORD_AUTO_FORWARD", "1").strip().lower() not in ("0", "false", "no", "off")
MODEL = os.environ.get("MODEL", "gpt-4o-mini")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "800"))
RATE_PER_MINUTE = int(os.environ.get("RATE_PER_MINUTE", "8"))
GUEST_PER_DAY = int(os.environ.get("GUEST_PER_DAY", os.environ.get("RATE_PER_DAY", "20")))
USER_PER_DAY = int(os.environ.get("USER_PER_DAY", "100"))


def _numero(nome, predefinito):
    try:
        return float(os.environ.get(nome, predefinito))
    except ValueError:
        return float(predefinito)


PRICE_IN_PER_M = _numero("PRICE_IN_PER_M", 0.15)    # dollari per milione di token (gpt-4o-mini)
PRICE_OUT_PER_M = _numero("PRICE_OUT_PER_M", 0.60)
DISCORD_ADMIN_ALERTS = os.environ.get("DISCORD_ADMIN_ALERTS", "1").strip().lower() not in ("0", "false", "no", "off")
ALLOWED_ORIGINS = [
    o.strip().rstrip("/").lower()
    for o in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ACCOUNTS_ATTIVI = bool(SUPABASE_URL and SUPABASE_ANON_KEY and SUPABASE_SERVICE_KEY)

MAX_MESSAGES = 30          # quanti messaggi di cronologia si accettano
MAX_CHARS_MESSAGE = 4000   # lunghezza massima di un singolo messaggio
MAX_CHARS_TOTAL = 20000    # lunghezza massima di tutta la conversazione
ALLOWED_ROLES = {"user", "assistant"}  # niente "system" dal browser
BAN_PERMANENTE = datetime(2999, 12, 31, tzinfo=timezone.utc)

if not ALLOWED_ORIGINS:
    app.logger.warning("ALLOWED_ORIGINS non impostata: CORS aperto a tutti i siti.")
if not OPENROUTER_API_KEY:
    app.logger.warning("OPENROUTER_API_KEY non impostata: /chat risponderà con errore.")
if not DISCORD_WEBHOOK_URL:
    app.logger.warning("DISCORD_WEBHOOK_URL non impostata: le domande non vengono inviate a Discord.")
if not ACCOUNTS_ATTIVI:
    app.logger.warning("Supabase non configurato: account, chat salvate e admin disattivati.")
    # Diagnostica: SOLO i nomi (mai i valori) delle variabili d'ambiente rilevanti. repr() fa
    # vedere spazi o caratteri invisibili nei nomi, la causa più comune di questo problema.
    _mancanti = [n for n, v in (("SUPABASE_URL", SUPABASE_URL),
                                ("SUPABASE_ANON_KEY", SUPABASE_ANON_KEY),
                                ("SUPABASE_SERVICE_KEY", SUPABASE_SERVICE_KEY)) if not v]
    _simili = sorted(repr(k) for k in os.environ
                     if "SUPA" in k.upper() or "ALLOW" in k.upper() or "ORIGIN" in k.upper())
    app.logger.warning("Variabili vuote o mancanti: %s", ", ".join(_mancanti) or "nessuna")
    app.logger.warning("Nomi simili trovati nell'ambiente: %s", ", ".join(_simili) or "nessuno")
    # Quale servizio Render sta girando davvero (id e nome non sono segreti)
    app.logger.warning("Servizio Render in esecuzione: id=%s nome=%s",
                       os.environ.get("RENDER_SERVICE_ID", "?"), os.environ.get("RENDER_SERVICE_NAME", "?"))
    # Tutti i NOMI delle variabili ricevute (mai i valori)
    app.logger.warning("Tutte le variabili ricevute (solo nomi): %s",
                       ", ".join(sorted(repr(k) for k in os.environ)))


# --- CORS ---------------------------------------------------------------------

def _origin_consentita(origin):
    if not origin:
        return False
    if not ALLOWED_ORIGINS:
        return True
    return origin.rstrip("/").lower() in ALLOWED_ORIGINS


@app.before_request
def preflight():
    if request.method == "OPTIONS":
        return "", 204


@app.after_request
def aggiungi_cors(resp):
    origin = request.headers.get("Origin")
    if _origin_consentita(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin if ALLOWED_ORIGINS else "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Max-Age"] = "600"
        resp.headers["Vary"] = "Origin"
    return resp


# --- Limite di richieste (in memoria) -------------------------------------------
# Serve per il "al minuto" e per il "al giorno" degli ospiti. Il limite giornaliero
# degli utenti registrati NON sta qui: si conta nel database (sopravvive ai riavvii).

class LimitatoreRichieste:
    def __init__(self, al_minuto, al_giorno):
        self.al_minuto = al_minuto
        self.al_giorno = al_giorno
        self._minuto = defaultdict(deque)
        self._giorno = defaultdict(deque)
        self._lock = threading.Lock()

    def controlla(self, chiave):
        """Registra una richiesta. Restituisce (consentita, secondi_da_attendere, quale)."""
        adesso = time.time()
        with self._lock:
            m, g = self._minuto[chiave], self._giorno[chiave]
            while m and adesso - m[0] > 60:
                m.popleft()
            while g and adesso - g[0] > 86400:
                g.popleft()

            if len(m) >= self.al_minuto:
                return False, int(60 - (adesso - m[0])) + 1, "minuto"
            if len(g) >= self.al_giorno:
                return False, int(86400 - (adesso - g[0])) + 1, "giorno"

            m.append(adesso)
            g.append(adesso)

            if len(self._giorno) > 5000:  # pulizia, per non far crescere la memoria
                for k in [k for k, d in self._giorno.items() if not d]:
                    self._giorno.pop(k, None)
                    self._minuto.pop(k, None)
            return True, 0, ""


limite_ospiti = LimitatoreRichieste(RATE_PER_MINUTE, GUEST_PER_DAY)
limite_utenti = LimitatoreRichieste(RATE_PER_MINUTE, 10**9)  # solo "al minuto"
limite_condivisioni = LimitatoreRichieste(3, 15)  # pulsante "Invia su Discord": per IP, anti-spam
limite_feedback = LimitatoreRichieste(30, 2000)   # pollice su/giù: per utente
limite_link = LimitatoreRichieste(10, 200)        # creare/revocare link di condivisione: per utente
limite_letture = LimitatoreRichieste(30, 3000)    # aprire un link condiviso: per IP


# --- Supabase -------------------------------------------------------------------

class SupabaseNonDisponibile(Exception):
    pass


def _intestazioni_service(extra=None):
    h = {"apikey": SUPABASE_SERVICE_KEY, "Content-Type": "application/json"}
    # Le vecchie chiavi (service_role) sono JWT e vanno anche in Authorization;
    # le nuove chiavi "sb_secret_..." vanno solo in apikey.
    if SUPABASE_SERVICE_KEY.startswith("eyJ"):
        h["Authorization"] = f"Bearer {SUPABASE_SERVICE_KEY}"
    if extra:
        h.update(extra)
    return h


def sb(metodo, percorso, params=None, corpo=None, extra=None):
    """Chiamata REST a Supabase con la chiave segreta. Solleva SupabaseNonDisponibile se fallisce."""
    try:
        r = requests.request(
            metodo,
            f"{SUPABASE_URL}{percorso}",
            params=params,
            json=corpo,
            headers=_intestazioni_service(extra),
            timeout=8,
        )
    except requests.RequestException as e:
        raise SupabaseNonDisponibile(type(e).__name__)
    if r.status_code >= 400:
        raise SupabaseNonDisponibile(f"HTTP {r.status_code}")
    return r


_cache_token = {}    # sha256(token) -> (utente, scadenza)
_cache_profili = {}  # id utente -> (profilo, scadenza)
_cache_lock = threading.Lock()


def verifica_token(token):
    """Restituisce {'id','email'} se il token è valido, None se non lo è."""
    chiave = hashlib.sha256(token.encode()).hexdigest()
    adesso = time.time()
    with _cache_lock:
        voce = _cache_token.get(chiave)
        if voce and voce[1] > adesso:
            return voce[0]

    try:
        r = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"},
            timeout=8,
        )
    except requests.RequestException as e:
        raise SupabaseNonDisponibile(type(e).__name__)

    if r.status_code in (401, 403):
        return None
    if r.status_code != 200:
        raise SupabaseNonDisponibile(f"HTTP {r.status_code}")

    dati = r.json()
    utente = {"id": dati.get("id"), "email": dati.get("email")}
    if not utente["id"]:
        return None
    with _cache_lock:
        if len(_cache_token) > 2000:
            _cache_token.clear()
        _cache_token[chiave] = (utente, adesso + 60)
    return utente


_colonna_notifica = True  # diventa False se nel database manca profiles.notified_at (migrazione non fatta)


def carica_profilo(uid, usa_cache=True):
    global _colonna_notifica
    adesso = time.time()
    if usa_cache:
        with _cache_lock:
            voce = _cache_profili.get(uid)
            if voce and voce[1] > adesso:
                return voce[0]

    campi = "id,email,is_admin,banned_until,ban_reason,daily_limit"
    try:
        r = sb("GET", "/rest/v1/profiles", params={
            "id": f"eq.{uid}",
            "select": campi + (",notified_at" if _colonna_notifica else ""),
        })
    except SupabaseNonDisponibile as e:
        if _colonna_notifica and str(e) == "HTTP 400":  # colonna mancante: si va avanti senza avvisi
            _colonna_notifica = False
            app.logger.warning("Manca la colonna profiles.notified_at: esegui migrazione-admin.sql su Supabase.")
            return carica_profilo(uid, usa_cache=False)
        raise
    righe = r.json()
    profilo = righe[0] if righe else {"id": uid}
    with _cache_lock:
        if len(_cache_profili) > 2000:
            _cache_profili.clear()
        _cache_profili[uid] = (profilo, adesso + 10)  # il ban vale entro 10 secondi
    return profilo


def invalida_profilo(uid):
    with _cache_lock:
        _cache_profili.pop(uid, None)


def dimentica_utente(uid):
    """Dopo la cancellazione di un account: via dalle cache, anche i suoi token."""
    with _cache_lock:
        _cache_profili.pop(uid, None)
        for k in [k for k, (u, _) in _cache_token.items() if u.get("id") == uid]:
            _cache_token.pop(k, None)


def _data(valore):
    if not valore:
        return None
    try:
        d = datetime.fromisoformat(str(valore).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ban_attivo(profilo):
    fine = _data(profilo.get("banned_until"))
    if fine and fine > datetime.now(timezone.utc):
        return fine
    return None


def testo_ban(profilo, fine):
    if fine >= BAN_PERMANENTE - timedelta(days=1):
        testo = "Il tuo account è stato sospeso."
    else:
        quando = fine.astimezone(FUSO).strftime("%d/%m/%Y alle %H:%M")
        testo = f"Il tuo account è sospeso fino al {quando}."
    motivo = (profilo.get("ban_reason") or "").strip()
    return f"{testo} Motivo: {motivo}" if motivo else testo


def messaggi_ultime_24h(uid):
    da = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = sb("GET", "/rest/v1/usage_log",
           params={"user_id": f"eq.{uid}", "created_at": f"gte.{da}", "select": "id"},
           extra={"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"})
    totale = r.headers.get("Content-Range", "*/0").split("/")[-1]
    return int(totale) if totale.isdigit() else 0


def limite_giornaliero(profilo):
    valore = profilo.get("daily_limit")
    if isinstance(valore, int) and not isinstance(valore, bool) and valore >= 0:
        return int(valore)
    return impostazioni()["limits"].get("user_per_day", USER_PER_DAY)


# --- Lavori in secondo piano, avvisi Discord, registro azioni ---------------------

SYNC_BACKGROUND = False  # le prove automatiche lo mettono a True per avere risultati immediati
_avvisi_dati = {}


def in_background(funzione, *args):
    """Esegue funzione(*args) senza far aspettare chi ha fatto la richiesta. Non solleva mai."""
    def _lavoro():
        try:
            funzione(*args)
        except Exception as e:  # un avviso o un log non devono mai rompere nulla
            app.logger.warning("Lavoro in secondo piano non riuscito: %s", type(e).__name__)
    if SYNC_BACKGROUND:
        _lavoro()
    else:
        threading.Thread(target=_lavoro, daemon=True).start()


def avvisa_una_volta(chiave, testo):
    """Scrive un avviso nel log al massimo ogni 10 minuti (per non riempirlo)."""
    adesso = time.time()
    if adesso - _avvisi_dati.get(chiave, 0) > 600:
        _avvisi_dati[chiave] = adesso
        app.logger.warning(testo)


def _per_discord(testo):
    """Toglie i backtick (rovinerebbero il testo in codice) e gli a capo. Le menzioni (@everyone...)
    sono già bloccate da allowed_mentions."""
    return str(testo or "").replace("`", "'").replace("\n", " ")[:200]


def avviso_admin(testo):
    """Messaggio per l'admin sul canale Discord (nuovo utente, sospensione...)."""
    if not (DISCORD_WEBHOOK_URL and DISCORD_ADMIN_ALERTS):
        return

    def _invia():
        r = requests.post(DISCORD_WEBHOOK_URL,
                          json={"content": testo[:1800], "allowed_mentions": {"parse": []}}, timeout=5)
        if r.status_code >= 400:
            app.logger.warning("Discord ha rifiutato l'avviso admin: HTTP %s", r.status_code)
    in_background(_invia)


def registra_azione(admin, azione, target_id=None, target_email=None, dettagli=None):
    """Aggiunge una riga al registro delle azioni dell'admin (se la migrazione è stata fatta)."""
    corpo = {"admin_id": admin.get("id"), "admin_email": admin.get("email"), "action": azione,
             "target_id": target_id, "target_email": target_email, "details": (dettagli or "")[:300] or None}

    def _scrivi():
        try:
            sb("POST", "/rest/v1/admin_log", corpo=corpo)
        except SupabaseNonDisponibile as e:
            avvisa_una_volta("admin_log", f"Registro azioni non scritto ({e}): hai eseguito migrazione-admin.sql?")
    in_background(_scrivi)


def avvisa_se_nuovo(profilo):
    """Prima volta che un utente nuovo si fa vedere: avvisa l'admin su Discord, una volta sola."""
    if not _colonna_notifica or "notified_at" not in profilo or profilo.get("notified_at"):
        return
    uid, email = profilo.get("id"), profilo.get("email")

    def _lavoro():
        # "is.null" nel filtro: se due richieste arrivano insieme, solo una vince e avvisa
        r = sb("PATCH", "/rest/v1/profiles", params={"id": f"eq.{uid}", "notified_at": "is.null"},
               corpo={"notified_at": datetime.now(timezone.utc).isoformat()},
               extra={"Prefer": "return=representation"})
        if r.json():
            invalida_profilo(uid)
            avviso_admin(f"🆕 **Nuovo utente su MiczyAI:** `{_per_discord(email)}`")
    in_background(_lavoro)


def registra_token(uid, tin, tout):
    """Token usati da una risposta, per statistiche e costi. Anonimo per gli ospiti."""
    try:
        sb("POST", "/rest/v1/ai_usage", corpo={"user_id": uid, "kind": "user" if uid else "guest",
                                               "tokens_in": tin, "tokens_out": tout})
    except SupabaseNonDisponibile as e:
        avvisa_una_volta("ai_usage", f"Token non registrati ({e}): hai eseguito migrazione-admin.sql?")


def costo(tin, tout):
    """Stima in dollari: i prezzi si regolano con PRICE_IN_PER_M e PRICE_OUT_PER_M."""
    return round((tin or 0) / 1e6 * PRICE_IN_PER_M + (tout or 0) / 1e6 * PRICE_OUT_PER_M, 4)


# --- Impostazioni del sito (si cambiano dal pannello admin) -------------------------

PERSONALITA = {
    "normale": ("Normale", ""),
    "pirata": ("Pirata 🏴‍☠️", "Parla come un pirata: arrr, mozzo, tesori e mari in tempesta. Le informazioni "
                              "devono restare corrette e utili."),
    "professore": ("Professore", "Rispondi come un professore paziente e un po' teatrale: spiega passo passo, "
                                 "con esempi e qualche battuta."),
    "sarcastico": ("Sarcastico", "Sii sarcastico e ironico in modo leggero e mai offensivo, ma dai sempre "
                                 "una risposta giusta e utile."),
    "poeta": ("Poeta", "Rispondi in rima o con immagini poetiche, restando chiaro e corretto."),
    "nonno": ("Nonno", "Parla come un nonno affettuoso che racconta aneddoti, chiama l'utente \"caro\" o "
                       "\"cara\" e dà consigli pratici."),
    "robot": ("Robot", "Parla come un robot dei vecchi film: frasi precise, qualche BEEP e una ogni tanto "
                       "una piccola avaria comica."),
    "brevissimo": ("Brevissimo", "Rispondi sempre in al massimo due frasi corte."),
    "custom": ("Personalizzata", ""),  # il testo lo scrive l'admin
}

IMPOSTAZIONI_DEFAULT = {
    "maintenance": {"on": False, "message": "Stiamo facendo un po' di manutenzione: torniamo tra poco!"},
    "banner": {"on": False, "text": "", "kind": "info"},
    "limits": {},
    "personality": {"id": "normale", "custom": ""},
}
LIMITI_CAMPI = {"guest_per_day": (1, 100000), "user_per_day": (1, 100000), "per_minute": (1, 1000)}


def normalizza_impostazione(chiave, v):
    """Controlla un'impostazione. Restituisce (valore_pulito, errore)."""
    if chiave not in IMPOSTAZIONI_DEFAULT:
        return None, "Impostazione sconosciuta"
    if not isinstance(v, dict):
        return None, "Valore non valido"

    if chiave == "maintenance":
        on, msg = v.get("on"), v.get("message", "")
        if not isinstance(on, bool) or not isinstance(msg, str):
            return None, "Valore non valido"
        return {"on": on, "message": msg.strip()[:300] or IMPOSTAZIONI_DEFAULT["maintenance"]["message"]}, None

    if chiave == "banner":
        on, testo, tipo = v.get("on"), v.get("text", ""), v.get("kind", "info")
        if not isinstance(on, bool) or not isinstance(testo, str) or tipo not in ("info", "warn", "success"):
            return None, "Valore non valido"
        testo = testo.strip()[:300]
        if on and not testo:
            return None, "Scrivi il testo dell'avviso"
        return {"on": on, "text": testo, "kind": tipo}, None

    if chiave == "limits":
        puliti = {}
        for nome, (minimo, massimo) in LIMITI_CAMPI.items():
            valore = v.get(nome)
            if valore is None or valore == "":
                continue
            if isinstance(valore, bool) or not isinstance(valore, int) or not minimo <= valore <= massimo:
                return None, f"'{nome}' deve essere un intero tra {minimo} e {massimo}"
            puliti[nome] = valore
        return puliti, None

    # personality
    scelta, custom = v.get("id"), v.get("custom", "")
    if scelta not in PERSONALITA or not isinstance(custom, str):
        return None, "Personalità non valida"
    custom = custom.strip()[:500]
    if scelta == "custom" and not custom:
        return None, "Scrivi le istruzioni della personalità personalizzata"
    return {"id": scelta, "custom": custom}, None


_cache_impostazioni = {"valori": None, "scade": 0}


def impostazioni(forza=False):
    """Impostazioni correnti (cache di 15 secondi). Se il database non risponde si tiene l'ultimo valore
    noto, così una breve interruzione non spegne per sbaglio la manutenzione."""
    adesso = time.time()
    with _cache_lock:
        if not forza and _cache_impostazioni["valori"] is not None and _cache_impostazioni["scade"] > adesso:
            return _cache_impostazioni["valori"]
        precedenti = _cache_impostazioni["valori"]

    valori, riuscito = copy.deepcopy(IMPOSTAZIONI_DEFAULT), False
    if ACCOUNTS_ATTIVI:
        try:
            for riga in sb("GET", "/rest/v1/settings", params={"select": "key,value"}).json():
                pulito, _ = normalizza_impostazione(riga.get("key"), riga.get("value"))
                if pulito is not None:
                    valori[riga["key"]] = pulito
            riuscito = True
        except Exception as e:  # le impostazioni non devono mai rompere la chat
            avvisa_una_volta("settings", f"Impostazioni non lette ({type(e).__name__} {e}): hai eseguito "
                                         "migrazione-admin.sql? Uso i valori predefiniti.")
    else:
        riuscito = True
    if not riuscito and precedenti is not None:
        valori = precedenti
    with _cache_lock:
        _cache_impostazioni["valori"] = valori
        _cache_impostazioni["scade"] = adesso + (15 if riuscito else 5)
    return valori


def applica_limiti(imp):
    """I limiti scelti dal pannello valgono al posto di quelli delle variabili d'ambiente."""
    limiti = imp["limits"]
    limite_ospiti.al_minuto = limiti.get("per_minute", RATE_PER_MINUTE)
    limite_ospiti.al_giorno = limiti.get("guest_per_day", GUEST_PER_DAY)
    limite_utenti.al_minuto = limiti.get("per_minute", RATE_PER_MINUTE)


def prompt_di_sistema(imp):
    p = imp["personality"]
    testo = p["custom"] if p["id"] == "custom" else PERSONALITA.get(p["id"], ("", ""))[1]
    return "\n\n".join(x for x in (SYSTEM_PROMPT, testo) if x)


# --- Utilità -----------------------------------------------------------------

def errore(testo, codice):
    return jsonify({"response": testo}), codice


def errore_admin(testo, codice):
    return jsonify({"error": testo}), codice


def token_della_richiesta():
    intestazione = request.headers.get("Authorization", "")
    if intestazione.lower().startswith("bearer "):
        return intestazione[7:].strip() or None
    return None


def id_valido(valore):
    try:
        return str(uuid.UUID(str(valore)))
    except (ValueError, AttributeError, TypeError):
        return None


def pulisci_messaggi(data):
    """Controlla i messaggi arrivati dal browser. Restituisce (messaggi, errore)."""
    if not isinstance(data, dict):
        return None, "Richiesta non valida"

    messages = data.get("messages")
    if not messages:  # compatibilità con il vecchio formato {"prompt": "..."}
        prompt = data.get("prompt", "")
        if not isinstance(prompt, str) or not prompt.strip():
            return None, "Nessun prompt o messaggi ricevuti"
        messages = [{"role": "user", "content": prompt}]

    if not isinstance(messages, list):
        return None, "Formato dei messaggi non valido"

    puliti = []
    for m in messages[-MAX_MESSAGES:]:
        if not isinstance(m, dict):
            return None, "Formato dei messaggi non valido"
        ruolo, contenuto = m.get("role"), m.get("content")
        if ruolo not in ALLOWED_ROLES or not isinstance(contenuto, str):
            return None, "Formato dei messaggi non valido"
        if not contenuto.strip():
            continue
        if len(contenuto) > MAX_CHARS_MESSAGE:
            return None, f"Messaggio troppo lungo (massimo {MAX_CHARS_MESSAGE} caratteri)"
        puliti.append({"role": ruolo, "content": contenuto})

    if not puliti or puliti[-1]["role"] != "user":
        return None, "L'ultimo messaggio deve essere dell'utente"
    if sum(len(m["content"]) for m in puliti) > MAX_CHARS_TOTAL:
        return None, "Conversazione troppo lunga: cancella la cronologia e riprova"

    return puliti, None


def invia_a_discord(testo):
    """Manda la domanda su Discord, in un thread a parte per non rallentare la risposta."""
    if not DISCORD_WEBHOOK_URL:
        return

    def _invia():
        try:
            r = requests.post(
                DISCORD_WEBHOOK_URL,
                json={
                    "content": f"📨 **Nuova domanda a MiczyAI:**\n{testo[:1800]}",
                    "allowed_mentions": {"parse": []},  # niente @everyone / @here
                },
                timeout=5,
            )
            if r.status_code >= 400:
                # 404 = webhook cancellato (Discord lo fa se trova l'indirizzo in un repo pubblico),
                # 401/403 = indirizzo non valido, 429 = troppi messaggi
                app.logger.warning("Discord ha rifiutato il messaggio: HTTP %s", r.status_code)
        except requests.RequestException as e:
            app.logger.warning("Invio a Discord non riuscito: %s", type(e).__name__)

    threading.Thread(target=_invia, daemon=True).start()


def _taglia(testo, massimo):
    testo = testo.strip()
    return testo if len(testo) <= massimo else testo[: massimo - 1].rstrip() + "…"


def costruisci_embed(domanda, risposta):
    """Messaggio Discord ordinato: 👤 Utente / 🤖 Risposta. I campi di Discord arrivano a 1024
    caratteri e il totale a 6000: una risposta lunga continua in un secondo riquadro."""
    domanda, risposta = domanda.strip(), risposta.strip()
    resto = ""
    if len(risposta) > 1000:
        taglio = risposta.rfind(" ", 800, 1000)
        taglio = taglio if taglio != -1 else 1000
        resto, risposta = risposta[taglio:].strip(), risposta[:taglio].rstrip() + " …"

    embed = {
        "title": "💬 Domanda e risposta su MiczyAI",
        "color": 0xFF5CA8,
        "fields": [
            {"name": "👤 Utente", "value": _taglia(domanda, 1000) or "-"},
            {"name": "🤖 Risposta di MiczyAI", "value": risposta or "-"},
        ],
        "footer": {"text": "Inviata dall'utente con il pulsante \"Invia su Discord\""},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    embeds = [embed]
    if resto:
        embeds.append({"title": "🤖 …continua", "color": 0xFF5CA8, "description": _taglia(resto, 3500)})
    return embeds


@app.route("/share", methods=["POST"])
def condividi():
    """Pulsante "Invia su Discord": l'utente sceglie di mandare una domanda e la sua risposta."""
    if not DISCORD_WEBHOOK_URL:
        return errore("L'invio su Discord non è attivo.", 503)

    consentita, attesa, _ = limite_condivisioni.controlla(request.remote_addr or "sconosciuto")
    if not consentita:
        resp, codice = errore("Hai inviato troppe volte: riprova più tardi.", 429)
        resp.headers["Retry-After"] = str(attesa)
        return resp, codice

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return errore("Richiesta non valida", 400)
    domanda, risposta = data.get("question"), data.get("answer")
    if not (isinstance(domanda, str) and isinstance(risposta, str) and domanda.strip() and risposta.strip()):
        return errore("Domanda o risposta mancante.", 400)

    try:
        r = requests.post(
            DISCORD_WEBHOOK_URL,
            json={
                "embeds": costruisci_embed(domanda[:MAX_CHARS_MESSAGE], risposta[:MAX_CHARS_TOTAL]),
                "allowed_mentions": {"parse": []},  # niente @everyone / @here
            },
            timeout=5,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        stato = getattr(getattr(e, "response", None), "status_code", None)
        app.logger.warning("Condivisione su Discord non riuscita: %s %s", type(e).__name__, stato or "")
        return errore("Invio su Discord non riuscito. Riprova tra poco.", 502)
    return jsonify({"ok": True})


def salva_chat(uid, conversation_id, messages, risposta, rigenera):
    """Salva domanda e risposta e registra l'uso. Non fa mai fallire la chat: se qualcosa
    va storto restituisce l'id (anche None) e scrive solo un avviso nel log.
    Restituisce (id_chat, id_della_risposta_salvata)."""
    message_id = None
    try:
        # Prima l'uso: così il limite giornaliero vale anche se il resto del salvataggio fallisce
        sb("POST", "/rest/v1/usage_log", corpo={"user_id": uid})

        if not conversation_id:
            titolo = messages[-1]["content"].strip().replace("\n", " ")[:60] or "Nuova chat"
            r = sb("POST", "/rest/v1/conversations",
                   corpo={"user_id": uid, "title": titolo},
                   extra={"Prefer": "return=representation"})
            conversation_id = r.json()[0]["id"]

        nuovi = [{"conversation_id": conversation_id, "user_id": uid,
                  "role": "assistant", "content": risposta}]
        if rigenera:
            # sostituisce l'ultima risposta invece di aggiungerne un'altra
            r = sb("GET", "/rest/v1/messages", params={
                "conversation_id": f"eq.{conversation_id}", "role": "eq.assistant",
                "order": "id.desc", "limit": 1, "select": "id"})
            vecchi = r.json()
            if vecchi:
                sb("DELETE", "/rest/v1/messages", params={"id": f"eq.{vecchi[0]['id']}"})
        else:
            nuovi.insert(0, {"conversation_id": conversation_id, "user_id": uid,
                             "role": "user", "content": messages[-1]["content"]})

        r = sb("POST", "/rest/v1/messages", corpo=nuovi, extra={"Prefer": "return=representation"})
        try:  # l'id serve al sito per il pollice su/giù
            ids = [m["id"] for m in r.json() if m.get("role") == "assistant"]
            message_id = ids[-1] if ids else None
        except (ValueError, TypeError, KeyError, AttributeError):
            message_id = None
        sb("PATCH", "/rest/v1/conversations", params={"id": f"eq.{conversation_id}"},
           corpo={"updated_at": datetime.now(timezone.utc).isoformat()})
    except (SupabaseNonDisponibile, KeyError, IndexError, ValueError) as e:
        app.logger.warning("Salvataggio chat non riuscito: %s", e)
    return conversation_id, message_id


# --- Risposta dell'IA: errori, token, streaming ------------------------------------

def risposta_errore_ia(e):
    """Trasforma un errore di OpenRouter in una risposta per il sito."""
    if isinstance(e, requests.Timeout):
        app.logger.warning("OpenRouter: timeout")
        return errore("L'IA ci sta mettendo troppo. Riprova tra poco.", 504)
    if isinstance(e, requests.HTTPError):
        stato = e.response.status_code if e.response is not None else 0
        motivo = ""
        try:  # il motivo spiegato da OpenRouter (es. "No auth credentials found", "Insufficient credits")
            motivo = (e.response.text or "")[:300].replace("\n", " ")
        except Exception:
            pass
        app.logger.warning("OpenRouter: errore HTTP %s - %s", stato, motivo)
        if stato in (401, 403):  # aiuta a capire QUALE chiave sta usando il servizio (mai intera)
            k = OPENROUTER_API_KEY
            app.logger.warning("Chiave usata: lunghezza=%d, inizia con %r, finisce con %r, spazi/a capo ai bordi=%s",
                               len(k), k[:6], k[-4:], k != k.strip())
        if stato == 429:
            return errore("L'IA è molto richiesta in questo momento. Riprova tra poco.", 503)
        # il codice (401 chiave non valida, 402 crediti, 404 modello...) aiuta a capire cosa non va
        return errore(f"Errore nella chiamata all'IA (codice {stato}).", 502)
    app.logger.warning("OpenRouter: %s", type(e).__name__)
    return errore("Errore nella chiamata all'IA.", 502)


def registra_uso_risposta(uid, uso, messages_modello, answer):
    """Token usati da una risposta (statistiche e costi). Se OpenRouter non li dà si stimano dai caratteri."""
    if not (ACCOUNTS_ATTIVI and answer):
        return
    tin = uso.get("prompt_tokens")
    tout = uso.get("completion_tokens")
    if not isinstance(tin, int):
        tin = sum(len(m["content"]) for m in messages_modello) // 4
    if not isinstance(tout, int):
        tout = len(answer) // 4
    in_background(registra_token, uid, tin, tout)


def _evento(oggetto):
    return "data: " + json.dumps(oggetto, ensure_ascii=False) + "\n\n"


def flusso_risposta(resp, uid, conversation_id, messages, messages_modello, rigenera):
    """Inoltra al sito la risposta a poco a poco (Server-Sent Events). Eventi:
    {"delta": "testo"} per ogni pezzetto; alla fine {"done": true, "response": ..., "conversation_id": ...,
    "message_id": ...}; se qualcosa va storto {"error": "..."}. La chat si salva solo a risposta completa."""
    pezzi, uso = [], {}
    try:
        for riga in resp.iter_lines(decode_unicode=True):
            if not riga or not riga.startswith("data:"):
                continue
            dato = riga[5:].strip()
            if dato == "[DONE]":
                break
            try:
                blocco = json.loads(dato)
            except ValueError:
                continue
            if not isinstance(blocco, dict):
                continue
            if isinstance(blocco.get("error"), dict):  # errore a metà risposta
                app.logger.warning("OpenRouter: errore nel flusso - %s", str(blocco["error"].get("message"))[:200])
                yield _evento({"error": "La risposta si è interrotta. Riprova."})
                return
            if isinstance(blocco.get("usage"), dict):
                uso = blocco["usage"]
            for scelta in blocco.get("choices") or []:
                testo = (scelta.get("delta") or {}).get("content")
                if isinstance(testo, str) and testo:
                    pezzi.append(testo)
                    yield _evento({"delta": testo})
    except requests.RequestException as e:
        app.logger.warning("OpenRouter: flusso interrotto (%s)", type(e).__name__)
        yield _evento({"error": "La risposta si è interrotta. Riprova."})
        return
    finally:
        try:
            resp.close()
        except Exception:
            pass

    answer = "".join(pezzi)
    if not answer.strip():
        yield _evento({"error": "L'IA non ha risposto. Riprova."})
        return
    registra_uso_risposta(uid, uso, messages_modello, answer)
    finale = {"done": True, "response": answer}
    if uid:
        finale["conversation_id"], id_risposta = salva_chat(uid, conversation_id, messages, answer, rigenera)
        if id_risposta:
            finale["message_id"] = id_risposta
    yield _evento(finale)


# --- Rotta principale: chat -----------------------------------------------------

@app.route("/chat", methods=["POST"])
def chat():
    token = token_della_richiesta()
    uid = None
    profilo = {}

    # 1) Chi è? (account facoltativo: senza token si è ospiti)
    if token and ACCOUNTS_ATTIVI:
        try:
            utente = verifica_token(token)
            if not utente:
                return errore("Sessione scaduta: accedi di nuovo.", 401)
            uid = utente["id"]
            profilo = carica_profilo(uid)
        except SupabaseNonDisponibile as e:
            app.logger.warning("Supabase non disponibile: %s", e)
            return errore("Gli account non sono disponibili al momento. Riprova tra poco.", 503)

    # 2) Manutenzione (gli admin possono continuare a usare il bot) e impostazioni scelte dal pannello
    imp = impostazioni()
    applica_limiti(imp)
    if imp["maintenance"]["on"] and not profilo.get("is_admin"):
        return errore(imp["maintenance"]["message"], 503)

    # 3) Sospeso?
    if uid:
        fine = ban_attivo(profilo)
        if fine:
            return errore(testo_ban(profilo, fine), 403)
        avvisa_se_nuovo(profilo)

    # 4) Limiti
    if uid:
        consentita, attesa, _ = limite_utenti.controlla(uid)
    else:
        consentita, attesa, quale = limite_ospiti.controlla(request.remote_addr or "sconosciuto")
    if not consentita:
        if not uid and quale == "giorno":
            testo = "Hai finito i messaggi gratuiti di oggi. Accedi per averne di più."
        else:
            testo = "Troppe richieste: aspetta un po' e riprova."
        resp, codice = errore(testo, 429)
        resp.headers["Retry-After"] = str(attesa)
        return resp, codice

    if uid:
        try:
            massimo = limite_giornaliero(profilo)
            if messaggi_ultime_24h(uid) >= massimo:
                return errore(f"Hai raggiunto il limite di {massimo} messaggi nelle ultime 24 ore. "
                              "Riprova più tardi.", 429)
        except SupabaseNonDisponibile as e:
            app.logger.warning("Supabase non disponibile: %s", e)
            return errore("Gli account non sono disponibili al momento. Riprova tra poco.", 503)

    # 5) Messaggi
    data = request.get_json(silent=True)
    messages, problema = pulisci_messaggi(data)
    if problema:
        return errore(problema, 400)

    conversation_id = None
    rigenera = False
    if uid:
        richiesto = data.get("conversation_id")
        if richiesto:
            conversation_id = id_valido(richiesto)
            if not conversation_id:
                return errore("Chat non valida", 400)
            try:
                r = sb("GET", "/rest/v1/conversations", params={
                    "id": f"eq.{conversation_id}", "user_id": f"eq.{uid}", "select": "id"})
                if not r.json():
                    return errore("Chat non trovata", 404)
            except SupabaseNonDisponibile as e:
                app.logger.warning("Supabase non disponibile: %s", e)
                return errore("Gli account non sono disponibili al momento. Riprova tra poco.", 503)
            rigenera = data.get("regenerate") is True

    if not OPENROUTER_API_KEY:
        app.logger.error("OPENROUTER_API_KEY mancante")
        return errore("Il servizio non è configurato correttamente.", 500)

    if DISCORD_AUTO_FORWARD and not rigenera:
        invia_a_discord(messages[-1]["content"])

    istruzioni = prompt_di_sistema(imp)
    if istruzioni:
        messages_modello = [{"role": "system", "content": istruzioni}] + messages
    else:
        messages_modello = messages

    # 6) Il modello (risposta intera, oppure a poco a poco se il sito lo chiede con "stream": true)
    in_streaming = data.get("stream") is True
    try:
        corpo_ia = {"model": MODEL, "messages": messages_modello, "max_tokens": MAX_TOKENS}
        extra_richiesta = {}
        if in_streaming:
            corpo_ia.update(stream=True, stream_options={"include_usage": True})
            extra_richiesta["stream"] = True
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json=corpo_ia,
            timeout=(5, 45),  # connessione, lettura
            **extra_richiesta,
        )
        resp.raise_for_status()
        if not in_streaming:
            dati_ia = resp.json()
            answer = dati_ia.get("choices", [{}])[0].get("message", {}).get("content", "")
            uso = dati_ia.get("usage") if isinstance(dati_ia.get("usage"), dict) else {}
    except (requests.RequestException, ValueError) as e:
        return risposta_errore_ia(e)

    if in_streaming:
        return Response(
            flusso_risposta(resp, uid, conversation_id, messages, messages_modello, rigenera),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 7) Token usati (statistiche e costi)
    registra_uso_risposta(uid, uso, messages_modello, answer)

    # 8) Salvataggio (solo utenti registrati)
    risultato = {"response": answer}
    if uid and answer:
        risultato["conversation_id"], id_risposta = salva_chat(uid, conversation_id, messages, answer, rigenera)
        if id_risposta:
            risultato["message_id"] = id_risposta
    return jsonify(risultato)


# --- Pollice su/giù e condivisione di una chat ---------------------------------------

def richiedi_utente():
    """Restituisce (utente, None) se c'è un accesso valido, altrimenti (None, risposta_di_errore)."""
    if not ACCOUNTS_ATTIVI:
        return None, errore("Account non attivi", 404)
    token = token_della_richiesta()
    if not token:
        return None, errore("Accesso richiesto", 401)
    try:
        utente = verifica_token(token)
    except SupabaseNonDisponibile as e:
        app.logger.warning("Supabase non disponibile: %s", e)
        return None, errore("Gli account non sono disponibili al momento. Riprova tra poco.", 503)
    if not utente:
        return None, errore("Sessione scaduta: accedi di nuovo.", 401)
    return utente, None


def _troppe(attesa):
    resp, codice = errore("Troppe richieste: aspetta un po' e riprova.", 429)
    resp.headers["Retry-After"] = str(attesa)
    return resp, codice


def _manca_migrazione(e):
    if "HTTP 404" in str(e):
        app.logger.warning("Tabella mancante (%s): hai eseguito migrazione-chat.sql su Supabase?", e)
        return errore("Questa funzione non è ancora attiva. Riprova più tardi.", 503)
    return errore("Gli account non sono disponibili al momento. Riprova tra poco.", 503)


@app.route("/feedback", methods=["POST"])
def feedback():
    """Pollice su (1), giù (-1) o rimosso (0) su una risposta salvata dell'utente."""
    utente, err = richiedi_utente()
    if err:
        return err
    consentita, attesa, _ = limite_feedback.controlla(utente["id"])
    if not consentita:
        return _troppe(attesa)
    data = _corpo_json()
    mid, voto = data.get("message_id"), data.get("rating")
    if isinstance(mid, bool) or not isinstance(mid, int) or not 0 < mid < 2**62:
        return errore("Risposta non valida", 400)
    if isinstance(voto, bool) or voto not in (1, -1, 0):
        return errore("Voto non valido", 400)
    uid = utente["id"]
    try:
        # solo le risposte dell'IA nelle proprie chat
        righe = sb("GET", "/rest/v1/messages", params={
            "id": f"eq.{mid}", "user_id": f"eq.{uid}", "role": "eq.assistant", "select": "id"}).json()
        if not righe:
            return errore("Risposta non trovata", 404)
        if voto == 0:
            sb("DELETE", "/rest/v1/feedback", params={"user_id": f"eq.{uid}", "message_id": f"eq.{mid}"})
        else:
            sb("POST", "/rest/v1/feedback", params={"on_conflict": "user_id,message_id"},
               corpo={"user_id": uid, "message_id": mid, "rating": voto},
               extra={"Prefer": "resolution=merge-duplicates"})
    except SupabaseNonDisponibile as e:
        return _manca_migrazione(e)
    return jsonify({"ok": True, "rating": voto})


def _chat_dell_utente(uid, data):
    """Restituisce (id_chat, errore): la chat deve esistere ed essere dell'utente."""
    cid = id_valido(data.get("conversation_id"))
    if not cid:
        return None, errore("Chat non valida", 400)
    righe = sb("GET", "/rest/v1/conversations", params={"id": f"eq.{cid}", "user_id": f"eq.{uid}", "select": "id"}).json()
    if not righe:
        return None, errore("Chat non trovata", 404)
    return cid, None


@app.route("/chats/share", methods=["POST"])
def condividi_chat():
    """Crea (o restituisce) il link di sola lettura di una chat dell'utente."""
    utente, err = richiedi_utente()
    if err:
        return err
    consentita, attesa, _ = limite_link.controlla(utente["id"])
    if not consentita:
        return _troppe(attesa)
    uid = utente["id"]
    try:
        cid, err = _chat_dell_utente(uid, _corpo_json())
        if err:
            return err
        esistente = sb("GET", "/rest/v1/shared_chats", params={"conversation_id": f"eq.{cid}", "select": "token"}).json()
        if esistente:
            return jsonify({"token": esistente[0]["token"]})
        token = secrets.token_urlsafe(12)
        sb("POST", "/rest/v1/shared_chats", corpo={"token": token, "conversation_id": cid, "user_id": uid})
    except SupabaseNonDisponibile as e:
        return _manca_migrazione(e)
    return jsonify({"token": token})


@app.route("/chats/unshare", methods=["POST"])
def smetti_di_condividere():
    utente, err = richiedi_utente()
    if err:
        return err
    consentita, attesa, _ = limite_link.controlla(utente["id"])
    if not consentita:
        return _troppe(attesa)
    uid = utente["id"]
    try:
        cid, err = _chat_dell_utente(uid, _corpo_json())
        if err:
            return err
        sb("DELETE", "/rest/v1/shared_chats", params={"conversation_id": f"eq.{cid}", "user_id": f"eq.{uid}"})
    except SupabaseNonDisponibile as e:
        return _manca_migrazione(e)
    return jsonify({"ok": True})


TOKEN_LINK = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@app.route("/shared/<token>", methods=["GET"])
def chat_condivisa(token):
    """Pubblica: mostra una chat condivisa. Niente email né dati dell'autore, solo titolo e messaggi."""
    if not ACCOUNTS_ATTIVI:
        return errore("Account non attivi", 404)
    consentita, attesa, _ = limite_letture.controlla(request.remote_addr or "sconosciuto")
    if not consentita:
        return _troppe(attesa)
    if not TOKEN_LINK.match(token):
        return errore("Questo link non esiste o non è più condiviso.", 404)
    try:
        link = sb("GET", "/rest/v1/shared_chats", params={"token": f"eq.{token}", "select": "conversation_id"}).json()
        if not link:
            return errore("Questo link non esiste o non è più condiviso.", 404)
        cid = link[0]["conversation_id"]
        chat_riga = sb("GET", "/rest/v1/conversations", params={"id": f"eq.{cid}", "select": "title,created_at"}).json()
        messaggi = sb("GET", "/rest/v1/messages", params={
            "conversation_id": f"eq.{cid}", "order": "id.asc", "limit": 300,
            "select": "role,content,created_at"}).json()
    except SupabaseNonDisponibile as e:
        return _manca_migrazione(e)
    if not chat_riga:
        return errore("Questo link non esiste o non è più condiviso.", 404)
    resp = jsonify({"title": chat_riga[0].get("title"), "created_at": chat_riga[0].get("created_at"),
                    "messages": messaggi})
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


# --- Utente: chi sono -----------------------------------------------------------

@app.route("/me", methods=["GET"])
def me():
    """Dati dell'utente loggato, per mostrare nel sito nome, limite e link admin."""
    if not ACCOUNTS_ATTIVI:
        return errore_admin("Account non attivi", 404)
    token = token_della_richiesta()
    if not token:
        return errore_admin("Accesso richiesto", 401)
    try:
        utente = verifica_token(token)
        if not utente:
            return errore_admin("Sessione scaduta", 401)
        profilo = carica_profilo(utente["id"])
        usati = messaggi_ultime_24h(utente["id"])
    except SupabaseNonDisponibile as e:
        app.logger.warning("Supabase non disponibile: %s", e)
        return errore_admin("Account non disponibili al momento", 503)

    fine = ban_attivo(profilo)
    if not fine:
        avvisa_se_nuovo(profilo)
    return jsonify({
        "id": utente["id"],
        "email": utente["email"],
        "is_admin": bool(profilo.get("is_admin")),
        "banned": bool(fine),
        "ban_message": testo_ban(profilo, fine) if fine else None,
        "messages_24h": usati,
        "daily_limit": limite_giornaliero(profilo),
    })


# --- Pannello admin --------------------------------------------------------------
# Tutte le rotte /admin/* richiedono un utente con is_admin = true nel database.
# Il ruolo si assegna solo dal SQL Editor di Supabase (vedi schema.sql).

def richiedi_admin():
    """Restituisce (utente, None) se è admin, altrimenti (None, risposta_di_errore)."""
    if not ACCOUNTS_ATTIVI:
        return None, errore_admin("Account non attivi", 404)
    token = token_della_richiesta()
    if not token:
        return None, errore_admin("Accesso richiesto", 401)
    try:
        utente = verifica_token(token)
        if not utente:
            return None, errore_admin("Sessione scaduta", 401)
        profilo = carica_profilo(utente["id"], usa_cache=False)
    except SupabaseNonDisponibile as e:
        app.logger.warning("Supabase non disponibile: %s", e)
        return None, errore_admin("Account non disponibili al momento", 503)
    if not profilo.get("is_admin"):
        return None, errore_admin("Non autorizzato", 403)
    return utente, None


def _corpo_json():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _bersaglio(admin, data):
    """Controlla l'utente su cui si agisce. Restituisce (id, profilo, errore)."""
    uid = id_valido(data.get("user_id"))
    if not uid:
        return None, None, errore_admin("user_id non valido", 400)
    try:
        profilo = carica_profilo(uid, usa_cache=False)
    except SupabaseNonDisponibile:
        return None, None, errore_admin("Database non disponibile", 503)
    if not profilo.get("email") and not profilo.get("is_admin") and "banned_until" not in profilo:
        return None, None, errore_admin("Utente non trovato", 404)
    return uid, profilo, None


@app.route("/admin/users", methods=["GET"])
def admin_utenti():
    admin, err = richiedi_admin()
    if err:
        return err
    try:
        limite = max(1, min(int(request.args.get("limit", 50)), 200))
    except ValueError:
        limite = 50
    cerca = request.args.get("q", "").strip()[:100]
    for c in "*%,()":
        cerca = cerca.replace(c, "")

    params = {"select": "*", "order": "created_at.desc", "limit": limite}
    if cerca:
        params["email"] = f"ilike.*{cerca}*"
    try:
        righe = sb("GET", "/rest/v1/admin_user_stats", params=params).json()
    except SupabaseNonDisponibile as e:
        app.logger.warning("Supabase non disponibile: %s", e)
        return errore_admin("Database non disponibile", 503)
    return jsonify({"users": righe})


@app.route("/admin/ban", methods=["POST"])
def admin_ban():
    admin, err = richiedi_admin()
    if err:
        return err
    data = _corpo_json()
    uid, profilo, err = _bersaglio(admin, data)
    if err:
        return err
    if uid == admin["id"] or profilo.get("is_admin"):
        return errore_admin("Non puoi sospendere un admin (nemmeno te stesso)", 400)

    if data.get("permanent") is True:
        fine = BAN_PERMANENTE
    else:
        try:
            ore = float(data.get("hours"))
        except (TypeError, ValueError):
            return errore_admin("Indica 'hours' (numero di ore) oppure 'permanent': true", 400)
        if not 0 < ore <= 24 * 365:
            return errore_admin("'hours' deve essere tra 0 e 8760", 400)
        fine = datetime.now(timezone.utc) + timedelta(hours=ore)

    motivo = str(data.get("reason") or "").strip()[:200] or None
    try:
        sb("PATCH", "/rest/v1/profiles", params={"id": f"eq.{uid}"},
           corpo={"banned_until": fine.isoformat(), "ban_reason": motivo})
    except SupabaseNonDisponibile:
        return errore_admin("Database non disponibile", 503)
    invalida_profilo(uid)
    app.logger.info("ADMIN %s ha sospeso %s fino a %s", admin["id"], uid, fine.isoformat())
    durata = "per sempre" if fine >= BAN_PERMANENTE - timedelta(days=1) \
        else "fino al " + fine.astimezone(FUSO).strftime("%d/%m/%Y %H:%M")
    registra_azione(admin, "ban", uid, profilo.get("email"), f"{durata}" + (f" - {motivo}" if motivo else ""))
    avviso_admin(f"🚫 **Utente sospeso:** `{_per_discord(profilo.get('email'))}` ({durata})"
                 + (f"\nMotivo: {_per_discord(motivo)}" if motivo else ""))
    return jsonify({"ok": True, "banned_until": fine.isoformat()})


@app.route("/admin/unban", methods=["POST"])
def admin_unban():
    admin, err = richiedi_admin()
    if err:
        return err
    uid, profilo, err = _bersaglio(admin, _corpo_json())
    if err:
        return err
    try:
        sb("PATCH", "/rest/v1/profiles", params={"id": f"eq.{uid}"},
           corpo={"banned_until": None, "ban_reason": None})
    except SupabaseNonDisponibile:
        return errore_admin("Database non disponibile", 503)
    invalida_profilo(uid)
    app.logger.info("ADMIN %s ha riattivato %s", admin["id"], uid)
    registra_azione(admin, "unban", uid, profilo.get("email"))
    return jsonify({"ok": True})


@app.route("/admin/limit", methods=["POST"])
def admin_limite():
    """Cambia il limite di messaggi nelle 24 ore per un utente (null = limite standard)."""
    admin, err = richiedi_admin()
    if err:
        return err
    data = _corpo_json()
    uid, profilo, err = _bersaglio(admin, data)
    if err:
        return err

    valore = data.get("daily_limit")
    if valore is not None:
        if isinstance(valore, bool) or not isinstance(valore, int) or not 0 <= valore <= 100000:
            return errore_admin("'daily_limit' deve essere un intero tra 0 e 100000 (o null)", 400)
    try:
        sb("PATCH", "/rest/v1/profiles", params={"id": f"eq.{uid}"}, corpo={"daily_limit": valore})
    except SupabaseNonDisponibile:
        return errore_admin("Database non disponibile", 503)
    invalida_profilo(uid)
    app.logger.info("ADMIN %s ha impostato il limite di %s a %s", admin["id"], uid, valore)
    registra_azione(admin, "limit", uid, profilo.get("email"),
                    "limite standard" if valore is None else f"{valore} messaggi / 24 ore")
    return jsonify({"ok": True, "daily_limit": valore})


@app.route("/admin/messages", methods=["GET"])
def admin_messaggi():
    """Ultimi messaggi di un utente, per decidere se sospenderlo."""
    admin, err = richiedi_admin()
    if err:
        return err
    uid = id_valido(request.args.get("user_id"))
    if not uid:
        return errore_admin("user_id non valido", 400)
    try:
        limite = max(1, min(int(request.args.get("limit", 50)), 200))
    except ValueError:
        limite = 50
    try:
        righe = sb("GET", "/rest/v1/messages", params={
            "user_id": f"eq.{uid}", "order": "id.desc", "limit": limite,
            "select": "id,conversation_id,role,content,created_at"}).json()
    except SupabaseNonDisponibile:
        return errore_admin("Database non disponibile", 503)
    return jsonify({"messages": righe})


# --- Pannello admin: statistiche, ricerca, impostazioni, registro -----------------------

def rpc(nome, corpo=None):
    """Chiama una funzione SQL della migrazione. Restituisce il JSON (None se vuoto)."""
    r = sb("POST", f"/rest/v1/rpc/{nome}", corpo=corpo or {})
    try:
        return r.json()
    except ValueError:
        return None


def _errore_db(e):
    if "HTTP 404" in str(e):  # funzione o tabella mancante
        return errore_admin("Mancano le tabelle nuove: esegui migrazione-admin.sql nel SQL Editor di Supabase.", 503)
    return errore_admin("Database non disponibile", 503)


def _giorni(predefinito=30):
    try:
        return max(1, min(int(request.args.get("days", predefinito)), 365))
    except ValueError:
        return predefinito


def _numeri(dati):
    return dati if isinstance(dati, dict) else {}


@app.route("/admin/overview", methods=["GET"])
def admin_panoramica():
    admin, err = richiedi_admin()
    if err:
        return err
    try:
        dati = rpc("admin_overview")
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    if not isinstance(dati, dict):
        return errore_admin("Risposta non valida dal database", 502)
    t = _numeri(dati.get("tokens"))
    dati["cost"] = {
        "usd": {p: costo(t.get("in_" + p), t.get("out_" + p)) for p in ("24h", "7d", "30d", "total")},
        "price_in_per_m": PRICE_IN_PER_M, "price_out_per_m": PRICE_OUT_PER_M,
        "model": MODEL, "estimate": True,
    }
    return jsonify(dati)


@app.route("/admin/daily", methods=["GET"])
def admin_giornaliero():
    admin, err = richiedi_admin()
    if err:
        return err
    try:
        giorni = rpc("admin_daily", {"p_days": _giorni()})
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    if not isinstance(giorni, list):
        return errore_admin("Risposta non valida dal database", 502)
    for g in giorni:
        g["cost"] = costo(g.get("tokens_in"), g.get("tokens_out"))
    return jsonify({"days": giorni})


@app.route("/admin/heatmap", methods=["GET"])
def admin_mappa():
    admin, err = richiedi_admin()
    if err:
        return err
    try:
        celle = rpc("admin_heatmap", {"p_days": _giorni()})
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    return jsonify({"cells": celle if isinstance(celle, list) else []})


@app.route("/admin/top", methods=["GET"])
def admin_classifica():
    admin, err = richiedi_admin()
    if err:
        return err
    try:
        giorni = max(0, min(int(request.args.get("days", 30)), 3650))
    except ValueError:
        giorni = 30
    try:
        righe = rpc("admin_top", {"p_days": giorni, "p_limit": 10})
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    return jsonify({"top": righe if isinstance(righe, list) else []})


@app.route("/admin/random-message", methods=["GET"])
def admin_domanda_strana():
    """Una domanda a caso tra quelle di oggi: per il riquadro "domanda più strana del giorno"."""
    admin, err = richiedi_admin()
    if err:
        return err
    try:
        ore = max(1, min(int(request.args.get("hours", 24)), 24 * 365))
    except ValueError:
        ore = 24
    try:
        riga = rpc("admin_random_message", {"p_hours": ore})
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    return jsonify({"message": riga if isinstance(riga, dict) else None})


@app.route("/admin/search", methods=["GET"])
def admin_cerca():
    admin, err = richiedi_admin()
    if err:
        return err
    q = request.args.get("q", "").strip()[:100]
    if len(q) < 2:
        return errore_admin("Scrivi almeno 2 caratteri", 400)
    try:
        righe = rpc("admin_search_messages", {"p_q": q, "p_limit": 50})
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    righe = righe if isinstance(righe, list) else []
    for r in righe:
        if isinstance(r.get("content"), str):
            r["content"] = r["content"][:600]
    return jsonify({"results": righe, "query": q})


@app.route("/admin/log", methods=["GET"])
def admin_registro():
    admin, err = richiedi_admin()
    if err:
        return err
    try:
        limite = max(1, min(int(request.args.get("limit", 100)), 300))
    except ValueError:
        limite = 100
    try:
        righe = sb("GET", "/rest/v1/admin_log", params={
            "select": "id,admin_email,action,target_email,details,created_at",
            "order": "id.desc", "limit": limite}).json()
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    return jsonify({"log": righe})


@app.route("/admin/feedback", methods=["GET"])
def admin_feedback():
    """Quante risposte hanno ricevuto 👍 e 👎, e le ultime con domanda e risposta."""
    admin, err = richiedi_admin()
    if err:
        return err
    try:
        dati = rpc("admin_feedback_summary", {"p_limit": 60})
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    if not isinstance(dati, dict):
        return errore_admin("Risposta non valida dal database", 502)
    for r in dati.get("recent") or []:
        for campo in ("question", "answer"):
            if isinstance(r.get(campo), str):
                r[campo] = r[campo][:800]
    return jsonify(dati)


def _descrivi_impostazione(chiave, v):
    if chiave == "maintenance":
        return "manutenzione " + ("attivata" if v["on"] else "disattivata")
    if chiave == "banner":
        return ("avviso acceso: " + v["text"][:80]) if v["on"] else "avviso spento"
    if chiave == "limits":
        return json.dumps(v, ensure_ascii=False) if v else "limiti standard"
    return "personalità: " + v["id"]


@app.route("/admin/settings", methods=["GET"])
def admin_impostazioni():
    admin, err = richiedi_admin()
    if err:
        return err
    return jsonify({
        "settings": impostazioni(forza=True),
        "personalities": [{"id": k, "label": v[0]} for k, v in PERSONALITA.items()],
        "defaults": {"guest_per_day": GUEST_PER_DAY, "user_per_day": USER_PER_DAY, "per_minute": RATE_PER_MINUTE},
    })


@app.route("/admin/settings", methods=["POST"])
def admin_salva_impostazione():
    admin, err = richiedi_admin()
    if err:
        return err
    data = _corpo_json()
    chiave = data.get("key")
    valore, problema = normalizza_impostazione(chiave, data.get("value"))
    if problema:
        return errore_admin(problema, 400)
    try:
        sb("POST", "/rest/v1/settings", params={"on_conflict": "key"},
           corpo={"key": chiave, "value": valore, "updated_at": datetime.now(timezone.utc).isoformat()},
           extra={"Prefer": "resolution=merge-duplicates"})
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    registra_azione(admin, "settings:" + chiave, dettagli=_descrivi_impostazione(chiave, valore))
    return jsonify({"ok": True, "settings": impostazioni(forza=True)})


@app.route("/admin/delete-user", methods=["POST"])
def admin_elimina_utente():
    """Cancella un account e tutti i suoi dati (chat, messaggi, conteggi). Non si può annullare."""
    admin, err = richiedi_admin()
    if err:
        return err
    data = _corpo_json()
    uid, profilo, err = _bersaglio(admin, data)
    if err:
        return err
    if uid == admin["id"] or profilo.get("is_admin"):
        return errore_admin("Non puoi eliminare un admin (nemmeno te stesso)", 400)
    email = (profilo.get("email") or "").strip()
    atteso = (email or uid).lower()
    if str(data.get("confirm_email") or "").strip().lower() != atteso:
        return errore_admin("Per confermare scrivi l'email dell'utente", 400)
    try:
        rpc("admin_delete_user", {"p_id": uid})
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    dimentica_utente(uid)
    app.logger.info("ADMIN %s ha eliminato l'account %s", admin["id"], uid)
    registra_azione(admin, "delete_user", uid, email or None, "account e dati cancellati")
    avviso_admin(f"🗑️ **Account eliminato:** `{_per_discord(email or uid)}`")
    return jsonify({"ok": True})


# --- Stato pubblico del sito ---------------------------------------------------------

@app.route("/status", methods=["GET"])
def stato_sito():
    """Letto da ogni pagina del sito: avviso in cima e modalità manutenzione."""
    imp = impostazioni()
    m, b = imp["maintenance"], imp["banner"]
    resp = jsonify({
        "maintenance": {"on": m["on"], "message": m["message"]},
        "banner": {"on": bool(b["on"] and b["text"]), "text": b["text"], "kind": b["kind"]},
    })
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# --- Salute --------------------------------------------------------------------

@app.route("/", methods=["GET"])
def home():
    return "Backend MiczyAI attivo su Render!"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "accounts": ACCOUNTS_ATTIVI})


if __name__ == "__main__":
    # Solo per prove in locale. Su Render si usa gunicorn (vedi README).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
