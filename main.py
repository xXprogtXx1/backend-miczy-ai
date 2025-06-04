from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app)  # Abilita CORS per tutte le origini

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json

    # Prova a prendere la cronologia dei messaggi, se non c'è usa solo prompt singolo
    messages = data.get("messages")
    if not messages:
        prompt = data.get("prompt", "")
        if not prompt:
            return jsonify({"response": "Nessun prompt o messaggi ricevuti"}), 400
        messages = [{"role": "user", "content": prompt}]

    openrouter_url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": "Bearer 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",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-4o-mini",
        "messages": messages
    }

    try:
        resp = requests.post(openrouter_url, headers=headers, json=payload)
        resp.raise_for_status()
        response_json = resp.json()
        answer = response_json.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        return jsonify({"response": f"Errore chiamata API: {str(e)}"}), 500

    return jsonify({"response": answer})

@app.route('/', methods=['GET'])
def home():
    return "Backend MiczyAI attivo su Render!"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port)
