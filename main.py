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
"""

import hashlib
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request
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
MODEL = os.environ.get("MODEL", "gpt-4o-mini")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "800"))
RATE_PER_MINUTE = int(os.environ.get("RATE_PER_MINUTE", "8"))
GUEST_PER_DAY = int(os.environ.get("GUEST_PER_DAY", os.environ.get("RATE_PER_DAY", "20")))
USER_PER_DAY = int(os.environ.get("USER_PER_DAY", "100"))
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


def carica_profilo(uid, usa_cache=True):
    adesso = time.time()
    if usa_cache:
        with _cache_lock:
            voce = _cache_profili.get(uid)
            if voce and voce[1] > adesso:
                return voce[0]

    r = sb("GET", "/rest/v1/profiles", params={
        "id": f"eq.{uid}",
        "select": "id,email,is_admin,banned_until,ban_reason,daily_limit",
    })
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
    return int(valore) if isinstance(valore, int) and valore >= 0 else USER_PER_DAY


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
            requests.post(
                DISCORD_WEBHOOK_URL,
                json={
                    "content": f"📨 **Nuova domanda a MiczyAI:**\n{testo[:1800]}",
                    "allowed_mentions": {"parse": []},  # niente @everyone / @here
                },
                timeout=5,
            )
        except requests.RequestException as e:
            app.logger.warning("Invio a Discord non riuscito: %s", type(e).__name__)

    threading.Thread(target=_invia, daemon=True).start()


def salva_chat(uid, conversation_id, messages, risposta, rigenera):
    """Salva domanda e risposta e registra l'uso. Non fa mai fallire la chat: se qualcosa
    va storto restituisce l'id (anche None) e scrive solo un avviso nel log."""
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

        sb("POST", "/rest/v1/messages", corpo=nuovi)
        sb("PATCH", "/rest/v1/conversations", params={"id": f"eq.{conversation_id}"},
           corpo={"updated_at": datetime.now(timezone.utc).isoformat()})
    except (SupabaseNonDisponibile, KeyError, IndexError, ValueError) as e:
        app.logger.warning("Salvataggio chat non riuscito: %s", e)
    return conversation_id


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

    # 2) Sospeso?
    if uid:
        fine = ban_attivo(profilo)
        if fine:
            return errore(testo_ban(profilo, fine), 403)

    # 3) Limiti
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

    # 4) Messaggi
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

    if not rigenera:
        invia_a_discord(messages[-1]["content"])

    if SYSTEM_PROMPT:
        messages_modello = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    else:
        messages_modello = messages

    # 5) Il modello
    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": MODEL, "messages": messages_modello, "max_tokens": MAX_TOKENS},
            timeout=(5, 45),  # connessione, lettura
        )
        resp.raise_for_status()
        answer = (
            resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        )
    except requests.Timeout:
        app.logger.warning("OpenRouter: timeout")
        return errore("L'IA ci sta mettendo troppo. Riprova tra poco.", 504)
    except requests.HTTPError as e:
        stato = e.response.status_code if e.response is not None else 0
        app.logger.warning("OpenRouter: errore HTTP %s", stato)
        if stato == 429:
            return errore("L'IA è molto richiesta in questo momento. Riprova tra poco.", 503)
        return errore("Errore nella chiamata all'IA.", 502)
    except (requests.RequestException, ValueError) as e:
        app.logger.warning("OpenRouter: %s", type(e).__name__)
        return errore("Errore nella chiamata all'IA.", 502)

    # 6) Salvataggio (solo utenti registrati)
    risultato = {"response": answer}
    if uid and answer:
        risultato["conversation_id"] = salva_chat(uid, conversation_id, messages, answer, rigenera)
    return jsonify(risultato)


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
    return jsonify({"ok": True, "banned_until": fine.isoformat()})


@app.route("/admin/unban", methods=["POST"])
def admin_unban():
    admin, err = richiedi_admin()
    if err:
        return err
    uid, _, err = _bersaglio(admin, _corpo_json())
    if err:
        return err
    try:
        sb("PATCH", "/rest/v1/profiles", params={"id": f"eq.{uid}"},
           corpo={"banned_until": None, "ban_reason": None})
    except SupabaseNonDisponibile:
        return errore_admin("Database non disponibile", 503)
    invalida_profilo(uid)
    app.logger.info("ADMIN %s ha riattivato %s", admin["id"], uid)
    return jsonify({"ok": True})


@app.route("/admin/limit", methods=["POST"])
def admin_limite():
    """Cambia il limite di messaggi nelle 24 ore per un utente (null = limite standard)."""
    admin, err = richiedi_admin()
    if err:
        return err
    data = _corpo_json()
    uid, _, err = _bersaglio(admin, data)
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
