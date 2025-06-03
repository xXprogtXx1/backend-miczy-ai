from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)  # Abilita CORS per tutte le origini

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"response": "Nessun prompt ricevuto"}), 400

    openrouter_url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": "Bearer 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",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}]
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
    return "Backend MiczyAI attivo!"

if __name__ == '__main__':
    # debug=False per sicurezza in produzione
    app.run(host='0.0.0.0', port=3000, debug=False)
