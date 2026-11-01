"""
Backend di MiczyAI (Flask) - versione con segreti in variabili d'ambiente.

Tutte le impostazioni arrivano dalle variabili d'ambiente di Render, MAI dal codice:

  OPENROUTER_API_KEY   (obbligatoria) chiave di OpenRouter
  DISCORD_WEBHOOK_URL  (facoltativa)  se vuota, nessun messaggio va su Discord
  ALLOWED_ORIGINS      (consigliata)  siti autorizzati, separati da virgola,
                                      es. https://tuonome.github.io
  MODEL                (facoltativa)  default: gpt-4o-mini
  MAX_TOKENS           (facoltativa)  lunghezza massima della risposta, default 800
  RATE_PER_MINUTE      (facoltativa)  richieste al minuto per IP, default 8
  RATE_PER_DAY         (facoltativa)  richieste al giorno per IP, default 150
  SYSTEM_PROMPT        (facoltativa)  istruzioni fisse messe prima della chat
"""

import os
import threading
import time
from collections import defaultdict, deque

import requests
from flask import Flask, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

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
RATE_PER_DAY = int(os.environ.get("RATE_PER_DAY", "150"))
ALLOWED_ORIGINS = [
    o.strip().rstrip("/").lower()
    for o in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]

MAX_MESSAGES = 30          # quanti messaggi di cronologia si accettano
MAX_CHARS_MESSAGE = 4000   # lunghezza massima di un singolo messaggio
MAX_CHARS_TOTAL = 20000    # lunghezza massima di tutta la conversazione
ALLOWED_ROLES = {"user", "assistant"}  # niente "system" dal browser

if not ALLOWED_ORIGINS:
    app.logger.warning("ALLOWED_ORIGINS non impostata: CORS aperto a tutti i siti.")
if not OPENROUTER_API_KEY:
    app.logger.warning("OPENROUTER_API_KEY non impostata: /chat risponderà con errore.")


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
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "600"
        resp.headers["Vary"] = "Origin"
    return resp


# --- Limite di richieste per IP (in memoria) -----------------------------------
# Con un solo processo (vedi README) basta; per più processi serve un database.

class LimitatoreRichieste:
    def __init__(self, al_minuto, al_giorno):
        self.al_minuto = al_minuto
        self.al_giorno = al_giorno
        self._minuto = defaultdict(deque)
        self._giorno = defaultdict(deque)
        self._lock = threading.Lock()

    def controlla(self, chiave):
        """Registra una richiesta. Restituisce (consentita, secondi_da_attendere)."""
        adesso = time.time()
        with self._lock:
            m, g = self._minuto[chiave], self._giorno[chiave]
            while m and adesso - m[0] > 60:
                m.popleft()
            while g and adesso - g[0] > 86400:
                g.popleft()

            if len(m) >= self.al_minuto:
                return False, int(60 - (adesso - m[0])) + 1
            if len(g) >= self.al_giorno:
                return False, int(86400 - (adesso - g[0])) + 1

            m.append(adesso)
            g.append(adesso)

            # pulizia ogni tanto, per non far crescere la memoria
            if len(self._giorno) > 5000:
                for k in [k for k, d in self._giorno.items() if not d]:
                    self._giorno.pop(k, None)
                    self._minuto.pop(k, None)
            return True, 0


limitatore = LimitatoreRichieste(RATE_PER_MINUTE, RATE_PER_DAY)


# --- Utilità -----------------------------------------------------------------

def errore(testo, codice):
    return jsonify({"response": testo}), codice


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
                    # evita che un utente faccia scattare @everyone / @here / menzioni
                    "allowed_mentions": {"parse": []},
                },
                timeout=5,
            )
        except requests.RequestException as e:
            app.logger.warning("Invio a Discord non riuscito: %s", type(e).__name__)

    threading.Thread(target=_invia, daemon=True).start()


# --- Rotte --------------------------------------------------------------------

@app.route("/chat", methods=["POST"])
def chat():
    consentita, attesa = limitatore.controlla(request.remote_addr or "sconosciuto")
    if not consentita:
        resp, codice = errore("Troppe richieste: aspetta un po' e riprova.", 429)
        resp.headers["Retry-After"] = str(attesa)
        return resp, codice

    messages, problema = pulisci_messaggi(request.get_json(silent=True))
    if problema:
        return errore(problema, 400)

    if not OPENROUTER_API_KEY:
        app.logger.error("OPENROUTER_API_KEY mancante")
        return errore("Il servizio non è configurato correttamente.", 500)

    invia_a_discord(messages[-1]["content"])

    if SYSTEM_PROMPT:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": MODEL, "messages": messages, "max_tokens": MAX_TOKENS},
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

    return jsonify({"response": answer})


@app.route("/", methods=["GET"])
def home():
    return "Backend MiczyAI attivo su Render!"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Solo per prove in locale. Su Render si usa gunicorn (vedi README).
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
