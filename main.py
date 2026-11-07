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
    PRICE_IN_PER_M          costo in dollari per milione di token in ingresso (stima costi), default 0.15
    PRICE_OUT_PER_M         costo in dollari per milione di token in uscita, default 0.60

  Streaming, pollice su/giù, link di condivisione e cartelle richiedono migrazione-chat.sql.

  Dal pannello admin (salvate nel database, non servono variabili): modalità manutenzione,
  avviso in cima al sito, limiti di messaggi, personalità del bot. Richiedono migrazione-admin.sql.
"""

import copy
import html
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
# Assistenza: email che ti avvisa dei nuovi ticket (servizio Resend, https://resend.com, piano gratuito).
# Senza RESEND_API_KEY o SUPPORT_NOTIFY_EMAIL i ticket vengono comunque salvati e li vedi nel pannello admin.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
SUPPORT_NOTIFY_EMAIL = os.environ.get("SUPPORT_NOTIFY_EMAIL", "").strip()
SUPPORT_FROM = os.environ.get("SUPPORT_FROM", "MiczyAI <onboarding@resend.dev>").strip()
# Indirizzo del sito (facoltativo, es. https://xxprogtxx1.github.io/miczyaitest/): nell'email compare il pulsante "Apri i ticket".
SITE_URL = os.environ.get("SITE_URL", "").strip().rstrip("/")
# "0" (predefinito, più sicuro): non si inoltra nulla da sé; su Discord arriva solo ciò che un utente
#     registrato invia col pulsante.
# "1": ogni domanda viene inoltrata a Discord in automatico (chiunque potrebbe farci arrivare testo).
DISCORD_AUTO_FORWARD = os.environ.get("DISCORD_AUTO_FORWARD", "0").strip().lower() not in ("0", "false", "no", "off")
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


    def stato(self, chiave):
        """Quante richieste al giorno ha già fatto questa chiave, senza registrarne una nuova.
        Restituisce (usate, massimo, secondi_al_rinnovo)."""
        adesso = time.time()
        with self._lock:
            g = self._giorno.get(chiave)
            if g is None:
                return 0, self.al_giorno, 0
            while g and adesso - g[0] > 86400:
                g.popleft()
            return len(g), self.al_giorno, (int(86400 - (adesso - g[0])) + 1 if g else 0)


limite_ospiti = LimitatoreRichieste(RATE_PER_MINUTE, GUEST_PER_DAY)
limite_utenti = LimitatoreRichieste(RATE_PER_MINUTE, 10**9)  # solo "al minuto"
limite_condivisioni = LimitatoreRichieste(2, 10)  # pulsante "Invia su Discord": per utente, 2 al minuto e 10 al giorno
limite_feedback = LimitatoreRichieste(30, 2000)   # pollice su/giù: per utente
limite_link = LimitatoreRichieste(10, 200)        # creare/revocare link di condivisione: per utente
limite_letture = LimitatoreRichieste(30, 3000)    # aprire un link condiviso: per IP
limite_health = LimitatoreRichieste(60, 5000)     # sveglia del server all'apertura del sito: per IP
limite_quota = LimitatoreRichieste(30, 3000)      # contatore messaggi degli ospiti: per IP
limite_importazioni = LimitatoreRichieste(2, 5)   # salvare nell'account la chat fatta da ospite: per utente
limite_ticket = LimitatoreRichieste(2, 5)         # aprire ticket: per utente, 2 al minuto e 5 al giorno
limite_ticket_letture = LimitatoreRichieste(20, 500)  # elenco dei propri ticket: per utente


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


# Lunghezza della risposta scelta dall'utente nelle impostazioni: (istruzione per l'IA, token massimi)
LUNGHEZZE = {
    "breve": ("Keep every answer very short: two or three sentences at most.", lambda m: min(m, 300)),
    "lunga": ("Give thorough, detailed answers, with examples when they help.", lambda m: min(m * 2, 2000)),
}


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
    """Pulsante "Invia su Discord". Solo per utenti registrati, e solo per testi che esistono davvero
    nelle loro chat salvate: così nessuno può usare questo indirizzo per scrivere testo a piacere
    nel canale Discord."""
    if not DISCORD_WEBHOOK_URL:
        return errore("L'invio su Discord non è attivo.", 503)

    utente, rifiuto = richiedi_utente()
    if rifiuto:
        return rifiuto

    consentita, attesa, _ = limite_condivisioni.controlla(utente["id"])
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
    domanda, risposta = domanda.strip()[:MAX_CHARS_MESSAGE], risposta.strip()[:MAX_CHARS_TOTAL]

    # i due testi devono essere messaggi salvati di questo utente (la domanda sua, la risposta del bot)
    try:
        for ruolo, testo in (("user", domanda), ("assistant", risposta)):
            r = sb("GET", "/rest/v1/messages", params={
                "user_id": f"eq.{utente['id']}", "role": f"eq.{ruolo}", "content": f"eq.{testo}",
                "imported": "eq.false", "select": "id", "limit": "1"})
            if not r.json():
                app.logger.warning("Condivisione rifiutata: testo non presente nelle chat di %s", utente["id"][:8])
                return errore("Puoi inviare solo messaggi delle tue chat salvate.", 403)
    except SupabaseNonDisponibile as e:
        app.logger.warning("Supabase non disponibile: %s", e)
        return errore("Gli account non sono disponibili al momento. Riprova tra poco.", 503)

    embeds = costruisci_embed(domanda, risposta)
    embeds[0]["footer"] = {"text": "Inviata con il pulsante \"Invia su Discord\" da " + str(utente.get("email") or utente["id"])[:80]}
    try:
        r = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"embeds": embeds, "allowed_mentions": {"parse": []}},  # niente @everyone / @here
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
        # Si leggono i byte e si decodifica qui in UTF-8: con decode_unicode "requests" userebbe
        # Latin-1 (text/event-stream senza charset) e accenti ed emoji diventerebbero "PerchÃ©".
        for riga in resp.iter_lines():
            riga = riga.decode("utf-8", errors="replace") if isinstance(riga, bytes) else riga
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
    max_token = MAX_TOKENS
    # la lunghezza è un'impostazione degli account: dagli ospiti viene ignorata
    scelta = data.get("length") if uid and isinstance(data.get("length"), str) else None
    if scelta in LUNGHEZZE:
        suggerimento, calcola = LUNGHEZZE[scelta]
        istruzioni = "\n\n".join(x for x in (istruzioni, suggerimento) if x)
        max_token = calcola(MAX_TOKENS)
    if istruzioni:
        messages_modello = [{"role": "system", "content": istruzioni}] + messages
    else:
        messages_modello = messages

    # 6) Il modello (risposta intera, oppure a poco a poco se il sito lo chiede con "stream": true)
    in_streaming = data.get("stream") is True
    try:
        corpo_ia = {"model": MODEL, "messages": messages_modello, "max_tokens": max_token}
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
        app.logger.warning("Tabella mancante (%s): hai eseguito i file .sql di migrazione su Supabase?", e)
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


@app.route("/quota", methods=["GET"])
def quota_ospite():
    """Quanti messaggi gratuiti restano a un ospite oggi (non ne consuma nessuno)."""
    consentita, attesa, _ = limite_quota.controlla(request.remote_addr or "sconosciuto")
    if not consentita:
        return _troppe(attesa)
    if token_della_richiesta() and ACCOUNTS_ATTIVI:
        return jsonify({"guest": False})
    applica_limiti(impostazioni())
    usati, massimo, rinnovo = limite_ospiti.stato(request.remote_addr or "sconosciuto")
    return jsonify({"guest": True, "remaining": max(0, massimo - usati), "limit": massimo, "reset": rinnovo})


MAX_IMPORT_MESSAGGI = 40
MAX_IMPORT_CARATTERI = 60000


@app.route("/chats/import", methods=["POST"])
def importa_chat():
    """Salva nell'account la conversazione fatta da ospite. I messaggi importati vengono marcati:
    il browser può scrivere ciò che vuole, quindi non valgono come "risposte vere" (es. per Discord)."""
    utente, rifiuto = richiedi_utente()
    if rifiuto:
        return rifiuto
    uid = utente["id"]
    try:
        profilo = carica_profilo(uid)
        fine = ban_attivo(profilo)
        if fine:
            return errore(testo_ban(profilo, fine), 403)
    except SupabaseNonDisponibile as e:
        app.logger.warning("Supabase non disponibile: %s", e)
        return errore("Gli account non sono disponibili al momento. Riprova tra poco.", 503)

    consentita, attesa, _ = limite_importazioni.controlla(uid)
    if not consentita:
        return _troppe(attesa)

    data = request.get_json(silent=True)
    grezzi = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(grezzi, list) or not (2 <= len(grezzi) <= MAX_IMPORT_MESSAGGI):
        return errore("Conversazione non valida.", 400)
    messaggi, totale = [], 0
    for m in grezzi:
        if not (isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)):
            return errore("Conversazione non valida.", 400)
        testo = m["content"].strip()
        if not testo or len(testo) > MAX_CHARS_TOTAL:
            return errore("Conversazione non valida.", 400)
        totale += len(testo)
        messaggi.append({"role": m["role"], "content": testo})
    if totale > MAX_IMPORT_CARATTERI or messaggi[0]["role"] != "user" or not any(m["role"] == "assistant" for m in messaggi):
        return errore("Conversazione non valida.", 400)

    conversation_id = None
    try:
        titolo = messaggi[0]["content"].replace("\n", " ")[:60] or "Nuova chat"
        r = sb("POST", "/rest/v1/conversations", corpo={"user_id": uid, "title": titolo},
               extra={"Prefer": "return=representation"})
        conversation_id = r.json()[0]["id"]
        righe = [{"conversation_id": conversation_id, "user_id": uid, "role": m["role"],
                  "content": m["content"], "imported": True} for m in messaggi]
        sb("POST", "/rest/v1/messages", corpo=righe)
        sb("PATCH", "/rest/v1/conversations", params={"id": f"eq.{conversation_id}"},
           corpo={"updated_at": datetime.now(timezone.utc).isoformat()})
    except (SupabaseNonDisponibile, KeyError, IndexError, ValueError) as e:
        app.logger.warning("Importazione chat non riuscita: %s", e)
        if conversation_id:  # niente chat vuote a metà
            try:
                sb("DELETE", "/rest/v1/conversations", params={"id": f"eq.{conversation_id}"})
            except SupabaseNonDisponibile:
                pass
        if "imported" in str(e) or "HTTP 400" in str(e):
            app.logger.warning("Hai eseguito migrazione-ospite.sql su Supabase?")
        return errore("Non sono riuscito a salvare la chat. Riprova più tardi.", 503)
    return jsonify({"conversation_id": conversation_id, "count": len(messaggi)})


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


@app.route("/admin/access", methods=["GET"])
def admin_access():
    """Elenco degli utenti per ultimo accesso (data, browser, sessioni). Mai l'IP."""
    admin, err = richiedi_admin()
    if err:
        return err
    q = (request.args.get("q") or "").strip()[:100]
    try:
        limite = max(1, min(int(request.args.get("limit", "100")), 200))
    except ValueError:
        limite = 100
    try:
        dati = rpc("admin_access_list", {"p_limit": limite, "p_query": q})
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    if not isinstance(dati, list):
        return errore_admin("Risposta non valida dal database", 502)
    utenti = []
    for r in dati:
        if not isinstance(r, dict):
            continue
        ua = r.get("user_agent")
        utenti.append({
            "id": r.get("id"),
            "email": r.get("email"),
            "created_at": r.get("created_at"),
            "last_sign_in_at": r.get("last_sign_in_at"),
            "sessions": r.get("sessions") or 0,
            "user_agent": ua[:300] if isinstance(ua, str) else None,
        })
    return jsonify({"users": utenti})


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
    return jsonify({"ok": True})


# --- Cancellazione dell'account: l'utente chiede (con un motivo), l'admin decide ------------

limite_account = LimitatoreRichieste(10, 60)   # richieste su /account/*: per utente


def _motivo_pulito(valore):
    """Il motivo scritto dall'utente: testo semplice, 3-500 caratteri. None se non va bene."""
    if not isinstance(valore, str):
        return None
    testo = " ".join(valore.split())
    return testo if 3 <= len(testo) <= 500 else None


def _richiesta_pubblica(r):
    return {"id": r.get("id"), "status": r.get("status"), "reason": r.get("reason"),
            "admin_note": r.get("admin_note"), "created_at": r.get("created_at"),
            "decided_at": r.get("decided_at")}


def _ultima_richiesta(uid):
    righe = sb("GET", "/rest/v1/deletion_requests", params={
        "user_id": f"eq.{uid}", "order": "created_at.desc", "limit": 1, "select": "*"}).json()
    return righe[0] if righe else None


@app.route("/account/delete-request", methods=["GET", "POST"])
def richiesta_cancellazione():
    """GET: com'è l'ultima richiesta. POST {reason}: chiede di eliminare il proprio account (decide l'admin)."""
    utente, err = richiedi_utente()
    if err:
        return err
    consentita, attesa, _ = limite_account.controlla(utente["id"])
    if not consentita:
        return _troppe(attesa)
    uid = utente["id"]
    try:
        if request.method == "GET":
            r = _ultima_richiesta(uid)
            return jsonify({"request": _richiesta_pubblica(r) if r else None})

        motivo = _motivo_pulito(_corpo_json().get("reason"))
        if not motivo:
            return errore("Scrivi il motivo (da 3 a 500 caratteri).", 400)
        profilo = carica_profilo(uid, usa_cache=False)
        if profilo.get("is_admin"):
            return errore("Un account admin non può essere eliminato da qui.", 400)
        ultima = _ultima_richiesta(uid)
        if ultima and ultima.get("status") == "pending":
            return errore("Hai già una richiesta in attesa.", 409)
        try:
            nuova = sb("POST", "/rest/v1/deletion_requests",
                       corpo={"user_id": uid, "email": utente.get("email"), "reason": motivo},
                       extra={"Prefer": "return=representation"}).json()
        except SupabaseNonDisponibile as e:
            if "HTTP 409" in str(e):   # due richieste insieme: vince la prima
                return errore("Hai già una richiesta in attesa.", 409)
            raise
    except SupabaseNonDisponibile as e:
        return _manca_migrazione(e)
    app.logger.info("Richiesta di cancellazione account %s", uid)
    return jsonify({"ok": True, "request": _richiesta_pubblica(nuova[0] if nuova else {"status": "pending"})})


@app.route("/account/delete-request/cancel", methods=["POST"])
def annulla_richiesta_cancellazione():
    """L'utente ci ripensa: ritira la richiesta ancora in attesa."""
    utente, err = richiedi_utente()
    if err:
        return err
    consentita, attesa, _ = limite_account.controlla(utente["id"])
    if not consentita:
        return _troppe(attesa)
    try:
        r = sb("PATCH", "/rest/v1/deletion_requests",
               params={"user_id": f"eq.{utente['id']}", "status": "eq.pending"},
               corpo={"status": "cancelled", "decided_at": datetime.now(timezone.utc).isoformat()},
               extra={"Prefer": "return=representation"}).json()
    except SupabaseNonDisponibile as e:
        return _manca_migrazione(e)
    if not r:
        return errore("Non c'è nessuna richiesta in attesa.", 404)
    return jsonify({"ok": True})


@app.route("/admin/deletion-requests", methods=["GET"])
def admin_richieste_cancellazione():
    """Richieste in attesa (tutte) e ultime già decise."""
    admin, err = richiedi_admin()
    if err:
        return err
    try:
        attesa = sb("GET", "/rest/v1/deletion_requests", params={
            "status": "eq.pending", "order": "created_at.asc", "limit": 100, "select": "*"}).json()
        decise = sb("GET", "/rest/v1/deletion_requests", params={
            "status": "neq.pending", "order": "created_at.desc", "limit": 30, "select": "*"}).json()
    except SupabaseNonDisponibile as e:
        if "HTTP 404" in str(e):
            return errore_admin("Manca la tabella: esegui migrazione-impostazioni.sql nel SQL Editor di Supabase.", 503)
        return errore_admin("Database non disponibile", 503)
    return jsonify({"pending": attesa, "decided": decise})


@app.route("/admin/deletion-requests/decide", methods=["POST"])
def admin_decidi_cancellazione():
    """Approva (e cancella davvero l'account) o rifiuta una richiesta."""
    admin, err = richiedi_admin()
    if err:
        return err
    data = _corpo_json()
    rid, approva = data.get("id"), data.get("approve")
    if isinstance(rid, bool) or not isinstance(rid, int) or not 0 < rid < 2**62:
        return errore_admin("Richiesta non valida", 400)
    if not isinstance(approva, bool):
        return errore_admin("Serve approve: true o false", 400)
    nota = data.get("note")
    nota = " ".join(nota.split())[:300] if isinstance(nota, str) and nota.strip() else None
    adesso = datetime.now(timezone.utc).isoformat()
    try:
        righe = sb("GET", "/rest/v1/deletion_requests", params={"id": f"eq.{rid}", "select": "*"}).json()
        if not righe:
            return errore_admin("Richiesta non trovata", 404)
        r = righe[0]
        if r.get("status") != "pending":
            return errore_admin("Questa richiesta è già stata gestita", 409)
        uid, email = r.get("user_id"), (r.get("email") or "").strip()

        if approva:
            if not uid:
                return errore_admin("L'account non esiste più", 409)
            profilo = carica_profilo(uid, usa_cache=False)
            if profilo.get("is_admin") or uid == admin["id"]:
                return errore_admin("Non puoi eliminare un admin (nemmeno te stesso)", 400)
            atteso = (email or uid).lower()
            if str(data.get("confirm_email") or "").strip().lower() != atteso:
                return errore_admin("Per confermare scrivi l'email dell'utente", 400)

        # si segna la decisione per prima: se due admin cliccano insieme, solo uno procede
        esito = sb("PATCH", "/rest/v1/deletion_requests", params={"id": f"eq.{rid}", "status": "eq.pending"},
                   corpo={"status": "approved" if approva else "rejected", "admin_note": nota,
                          "decided_at": adesso, "decided_by": admin.get("email")},
                   extra={"Prefer": "return=representation"}).json()
        if not esito:
            return errore_admin("Questa richiesta è già stata gestita", 409)

        if approva:
            try:
                rpc("admin_delete_user", {"p_id": uid})
            except SupabaseNonDisponibile:
                sb("PATCH", "/rest/v1/deletion_requests", params={"id": f"eq.{rid}"},
                   corpo={"status": "pending", "decided_at": None, "decided_by": None})   # non è successo niente
                raise
            dimentica_utente(uid)
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    registra_azione(admin, "deletion_approved" if approva else "deletion_rejected", uid, email or None,
                    (nota or "") or None)
    app.logger.info("ADMIN %s ha %s la richiesta di cancellazione %s", admin["id"],
                    "approvato" if approva else "rifiutato", rid)
    return jsonify({"ok": True})


# --- Stato pubblico del sito ---------------------------------------------------------

@app.route("/health", methods=["GET", "HEAD"])
def salute():
    """Leggerissimo: il sito lo chiama appena si apre, così Render si sveglia mentre l'utente legge.
    Non tocca il database e non richiede l'accesso."""
    consentita, attesa, _ = limite_health.controlla(request.remote_addr or "sconosciuto")
    if not consentita:
        return _troppe(attesa)
    resp = jsonify({"ok": True})
    resp.headers["Cache-Control"] = "no-store"
    return resp


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


# --- Assistenza tecnica: ticket ------------------------------------------------

CATEGORIE_TICKET = ("account", "chat", "bug", "idea", "altro")
MAX_TICKET_APERTI = 5


def _testo_ticket(valore, minimo, massimo, una_riga):
    """Testo scritto dall'utente: niente caratteri di controllo. Una riga sola per l'oggetto."""
    if not isinstance(valore, str):
        return None
    pulito = "".join(c for c in valore if c in "\n\t" or ord(c) >= 32)
    pulito = " ".join(pulito.split()) if una_riga else "\n".join(r.rstrip() for r in pulito.strip().splitlines())
    return pulito if minimo <= len(pulito) <= massimo else None


def html_email_ticket(numero, da, categoria, oggetto, messaggio, nuovo=True):
    """Versione grafica dell'email di avviso. Tutto il testo dell'utente passa da html.escape."""
    e = lambda v: html.escape(str(v or ""), quote=True)
    nomi = {"account": "Account e accesso", "chat": "Chat e risposte", "bug": "Qualcosa non funziona", "idea": "Un'idea", "altro": "Altro"}
    msg = e(messaggio).replace("\n", "<br>")
    titolo = "Nuovo ticket" if nuovo else "Nuova risposta al ticket"
    bottone = ""
    if SITE_URL.startswith("https://"):
        bottone = ('<tr><td align="center" style="padding:6px 0 22px"><a href="' + e(SITE_URL) + '/admin/" style="display:inline-block;'
                   'background:#ff5aa5;color:#17131f;font:800 16px Arial,Helvetica,sans-serif;text-decoration:none;padding:13px 26px;'
                   'border:3px solid #17131f;border-radius:999px;box-shadow:4px 4px 0 #17131f">Apri i ticket</a></td></tr>')
    return f'''<!DOCTYPE html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><title>{titolo} #{e(numero)}</title></head>
<body style="margin:0;padding:0;background:#fff3c4" bgcolor="#fff3c4">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{e(oggetto)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#fff3c4"><tr><td align="center" style="padding:28px 14px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:540px">
<tr><td align="center" style="padding:0 0 16px;font:900 28px Arial Black,Arial,Helvetica,sans-serif;color:#17131f;letter-spacing:-1px">Miczy<span style="background:#ff5aa5;padding:0 6px;border-radius:8px">AI</span></td></tr>
<tr><td bgcolor="#ffffff" style="background:#ffffff;border:3px solid #17131f;border-radius:22px;box-shadow:6px 6px 0 #17131f">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td bgcolor="#ffd93d" style="background:#ffd93d;border-bottom:3px solid #17131f;border-radius:19px 19px 0 0;padding:16px 24px;font:800 14px Arial,Helvetica,sans-serif;color:#17131f">🎫&nbsp; {titolo} #{e(numero)}</td></tr>
<tr><td style="padding:24px 26px 8px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td style="font:900 22px/1.25 Arial,Helvetica,sans-serif;color:#17131f;padding:0 0 12px">{e(oggetto)}</td></tr>
<tr><td style="padding:0 0 16px"><span style="display:inline-block;background:#e9e2ff;border:2px solid #17131f;border-radius:999px;padding:3px 12px;font:800 12px Arial,Helvetica,sans-serif;color:#17131f">{e(nomi.get(categoria, "Altro"))}</span>
&nbsp;<span style="font:13px Arial,Helvetica,sans-serif;color:#4a4458">da <b>{e(da)}</b></span></td></tr>
<tr><td style="font:16px/1.6 Arial,Helvetica,sans-serif;color:#2b2536;background:#faf7ff;border-left:5px solid #ff5aa5;border-radius:6px;padding:14px 16px">{msg}</td></tr>
<tr><td style="height:20px"></td></tr>
{bottone}
<tr><td style="font:13px/1.6 Arial,Helvetica,sans-serif;color:#4a4458;border-top:2px dashed #d9d3e3;padding:14px 0 22px">Rispondi dal pannello admin (scheda <b>Ticket</b>): l'utente vedrà la risposta nelle sue Impostazioni. Se rispondi a questa email, scrivi direttamente a {e(da)}.</td></tr>
</table></td></tr></table></td></tr>
<tr><td align="center" style="padding:16px 8px 0;font:12px/1.6 Arial,Helvetica,sans-serif;color:#6b6478">Notifica automatica di MiczyAI.</td></tr>
</table></td></tr></table></body></html>'''


def invia_email_assistenza(oggetto, testo, rispondi_a=None, html_corpo=None):
    """Ti scrive per un nuovo ticket (Resend). In background: se fallisce il ticket resta salvato."""
    if not (RESEND_API_KEY and SUPPORT_NOTIFY_EMAIL):
        return
    corpo = {"from": SUPPORT_FROM, "to": [SUPPORT_NOTIFY_EMAIL],
             "subject": " ".join(str(oggetto).split())[:150], "text": str(testo)[:5000]}
    if html_corpo:
        corpo["html"] = str(html_corpo)[:60000]
    if rispondi_a and re.fullmatch(r"[^\s@<>,;]+@[^\s@<>,;]+", rispondi_a):
        corpo["reply_to"] = rispondi_a

    def _invia():
        try:
            r = requests.post("https://api.resend.com/emails", json=corpo, timeout=10,
                              headers={"Authorization": "Bearer " + RESEND_API_KEY})
            if r.status_code >= 300:
                app.logger.warning("Email assistenza non inviata: HTTP %s", r.status_code)
        except requests.RequestException as e:
            app.logger.warning("Email assistenza non inviata: %s", type(e).__name__)
    in_background(_invia)


def _ticket_pubblico(t, messaggi=None):
    return {"id": t.get("id"), "category": t.get("category"), "subject": t.get("subject"),
            "message": t.get("message"), "status": t.get("status"), "last_author": t.get("last_author") or "user",
            "created_at": t.get("created_at"),
            "messages": [{"id": m.get("id"), "author": m.get("author"), "body": m.get("body"),
                          "created_at": m.get("created_at")} for m in (messaggi or [])]}


def _messaggi_dei_ticket(ids):
    """Le conversazioni dei ticket indicati: {ticket_id: [messaggi in ordine]}."""
    per_ticket = defaultdict(list)
    if not ids:
        return per_ticket
    righe = sb("GET", "/rest/v1/ticket_messages", params={
        "ticket_id": "in.(" + ",".join(str(int(i)) for i in ids) + ")", "order": "id.asc", "limit": 2000,
        "select": "id,ticket_id,author,body,created_at,author_email"}).json()
    for m in righe:
        per_ticket[m.get("ticket_id")].append(m)
    return per_ticket


@app.route("/tickets", methods=["GET", "POST"])
def ticket_utente():
    """GET: i tuoi ticket con le risposte. POST {subject, category, message}: apre un ticket e ti avvisa per email."""
    utente, err = richiedi_utente()
    if err:
        return err
    uid = utente["id"]
    limite = limite_ticket if request.method == "POST" else limite_ticket_letture
    consentita, attesa, _ = limite.controlla(uid)
    if not consentita:
        return _troppe(attesa)
    try:
        if request.method == "GET":
            righe = sb("GET", "/rest/v1/tickets", params={
                "user_id": f"eq.{uid}", "order": "created_at.desc", "limit": 20, "select": "*"}).json()
            conv = _messaggi_dei_ticket([t.get("id") for t in righe])
            return jsonify({"tickets": [_ticket_pubblico(t, conv.get(t.get("id"))) for t in righe]})

        dati = _corpo_json()
        oggetto = _testo_ticket(dati.get("subject"), 3, 100, True)
        messaggio = _testo_ticket(dati.get("message"), 10, 2000, False)
        categoria = dati.get("category") if dati.get("category") in CATEGORIE_TICKET else "altro"
        if not oggetto:
            return errore("Scrivi un oggetto (da 3 a 100 caratteri).", 400)
        if not messaggio:
            return errore("Descrivi il problema (da 10 a 2000 caratteri).", 400)
        aperti = sb("GET", "/rest/v1/tickets", params={
            "user_id": f"eq.{uid}", "status": "eq.open", "select": "id", "limit": MAX_TICKET_APERTI + 1}).json()
        if len(aperti) >= MAX_TICKET_APERTI:
            return errore("Hai già %d ticket aperti: aspetta una risposta prima di aprirne altri." % MAX_TICKET_APERTI, 409)
        nuovo = sb("POST", "/rest/v1/tickets",
                   corpo={"user_id": uid, "email": utente.get("email"), "category": categoria,
                          "subject": oggetto, "message": messaggio},
                   extra={"Prefer": "return=representation"}).json()
    except SupabaseNonDisponibile as e:
        return _manca_migrazione(e)
    t = nuovo[0] if nuovo else {"subject": oggetto, "message": messaggio, "category": categoria, "status": "open"}
    invia_email_assistenza(
        f"[MiczyAI] Nuovo ticket #{t.get('id', '?')}: {oggetto}",
        f"Da: {utente.get('email')}\nCategoria: {categoria}\nOggetto: {oggetto}\n\n{messaggio}\n\n"
        "Rispondi dal pannello admin (scheda Ticket): l'utente vedrà la risposta nelle Impostazioni.",
        rispondi_a=utente.get("email"),
        html_corpo=html_email_ticket(t.get("id", "?"), utente.get("email"), categoria, oggetto, messaggio))
    app.logger.info("Nuovo ticket %s da %s", t.get("id"), uid)
    return jsonify({"ok": True, "ticket": _ticket_pubblico(t)})


limite_ticket_msg = LimitatoreRichieste(6, 60)   # risposte dell'utente a un ticket: per utente
MAX_MESSAGGI_TICKET = 100


@app.route("/tickets/<int:tid>/messages", methods=["POST"])
def ticket_rispondi(tid):
    """L'utente risponde al proprio ticket, finché è aperto. Corpo: {body}."""
    utente, err = richiedi_utente()
    if err:
        return err
    uid = utente["id"]
    consentita, attesa, _ = limite_ticket_msg.controlla(uid)
    if not consentita:
        return _troppe(attesa)
    testo = _testo_ticket(_corpo_json().get("body"), 1, 2000, False)
    if not testo:
        return errore("Scrivi un messaggio (massimo 2000 caratteri).", 400)
    try:
        righe = sb("GET", "/rest/v1/tickets", params={"id": f"eq.{tid}", "user_id": f"eq.{uid}", "select": "*"}).json()
        if not righe:
            return errore("Ticket non trovato.", 404)
        t = righe[0]
        if t.get("status") != "open":
            return errore("Questo ticket è chiuso: aprine uno nuovo se ti serve ancora aiuto.", 409)
        gia = sb("GET", "/rest/v1/ticket_messages", params={"ticket_id": f"eq.{tid}", "select": "id", "limit": MAX_MESSAGGI_TICKET + 1}).json()
        if len(gia) >= MAX_MESSAGGI_TICKET:
            return errore("Questa conversazione è molto lunga: apri un nuovo ticket.", 409)
        nuovo = sb("POST", "/rest/v1/ticket_messages",
                   corpo={"ticket_id": tid, "author": "user", "author_email": utente.get("email"), "body": testo},
                   extra={"Prefer": "return=representation"}).json()
        sb("PATCH", "/rest/v1/tickets", params={"id": f"eq.{tid}"}, corpo={"last_author": "user"})
    except SupabaseNonDisponibile as e:
        return _manca_migrazione(e)
    invia_email_assistenza(
        f"[MiczyAI] Nuova risposta al ticket #{tid}: {t.get('subject')}",
        f"Da: {utente.get('email')}\nTicket #{tid}: {t.get('subject')}\n\n{testo}\n\n"
        "Rispondi dal pannello admin (scheda Ticket).",
        rispondi_a=utente.get("email"),
        html_corpo=html_email_ticket(tid, utente.get("email"), t.get("category"), t.get("subject"), testo, nuovo=False))
    m = nuovo[0] if nuovo else {"author": "user", "body": testo}
    return jsonify({"ok": True, "message": {"id": m.get("id"), "author": "user", "body": m.get("body"),
                                            "created_at": m.get("created_at")}})


@app.route("/admin/tickets", methods=["GET"])
def admin_ticket():
    """Elenco dei ticket: ?status=open (predefinito), closed o all."""
    admin, err = richiedi_admin()
    if err:
        return err
    stato = request.args.get("status", "open")
    params = {"order": "created_at.desc", "limit": 100, "select": "*"}
    if stato in ("open", "closed"):
        params["status"] = f"eq.{stato}"
    try:
        righe = sb("GET", "/rest/v1/tickets", params=params).json()
        aperti = sb("GET", "/rest/v1/tickets", params={"status": "eq.open", "select": "id", "limit": 500}).json()
    except SupabaseNonDisponibile as e:
        if "HTTP 404" in str(e) or "HTTP 400" in str(e):
            return errore_admin("Manca la tabella: esegui migrazione-ticket.sql e migrazione-ticket2.sql nel SQL Editor di Supabase.", 503)
        return errore_admin("Database non disponibile", 503)
    try:
        conv = _messaggi_dei_ticket([t.get("id") for t in righe])
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    for t in righe:
        t["messages"] = [{"id": m.get("id"), "author": m.get("author"), "author_email": m.get("author_email"),
                          "body": m.get("body"), "created_at": m.get("created_at")} for m in conv.get(t.get("id"), [])]
    return jsonify({"tickets": righe, "open_count": len(aperti)})


@app.route("/admin/tickets/update", methods=["POST"])
def admin_ticket_aggiorna():
    """Risponde a un ticket e/o lo chiude o lo riapre. Corpo: {id, reply?, status?}."""
    admin, err = richiedi_admin()
    if err:
        return err
    dati = _corpo_json()
    tid = dati.get("id")
    if isinstance(tid, bool) or not isinstance(tid, int) or not 0 < tid < 2**62:
        return errore_admin("Ticket non valido", 400)
    modifiche = {}
    risposta = dati.get("reply")
    testo_risposta = None
    if risposta is not None:
        testo_risposta = _testo_ticket(risposta, 1, 2000, False)
        if not testo_risposta:
            return errore_admin("La risposta deve avere da 1 a 2000 caratteri", 400)
        modifiche["last_author"] = "admin"
    nuovo_stato = dati.get("status")
    if nuovo_stato is not None:
        if nuovo_stato not in ("open", "closed"):
            return errore_admin("Stato non valido", 400)
        modifiche["status"] = nuovo_stato
        modifiche["closed_at"] = datetime.now(timezone.utc).isoformat() if nuovo_stato == "closed" else None
    if not modifiche:
        return errore_admin("Niente da cambiare", 400)
    try:
        esito = sb("PATCH", "/rest/v1/tickets", params={"id": f"eq.{tid}"}, corpo=modifiche,
                   extra={"Prefer": "return=representation"}).json()
        if not esito:
            return errore_admin("Ticket non trovato", 404)
        if testo_risposta:
            sb("POST", "/rest/v1/ticket_messages",
               corpo={"ticket_id": tid, "author": "admin", "author_email": admin.get("email"), "body": testo_risposta})
    except SupabaseNonDisponibile as e:
        return _errore_db(e)
    t = esito[0]
    registra_azione(admin, "ticket", t.get("user_id"), t.get("email"),
                    f"ticket #{tid}: " + ("risposta" if testo_risposta else "") +
                    (" " if testo_risposta and "status" in modifiche else "") +
                    (("chiuso" if nuovo_stato == "closed" else "riaperto") if "status" in modifiche else ""))
    return jsonify({"ok": True, "ticket": t})


if __name__ == "__main__":
    # Solo per prove in locale. Su Render si usa gunicorn (vedi README).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
