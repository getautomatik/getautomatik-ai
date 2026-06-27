from flask import Flask, jsonify, request
from agents import create_hunter, create_closer, create_delivery, create_analyst
from supabase import create_client
from dotenv import load_dotenv
import anthropic
import os
import json
import threading
import time
from datetime import datetime
import requests as req

load_dotenv()

app = Flask(__name__)
db = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
claude = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))

hunter = create_hunter()
closer_agent = create_closer()
delivery_agent = create_delivery()
analyst = create_analyst()

AGENTS = {"Hunter": hunter, "Closer": closer_agent, "Delivery": delivery_agent, "Analyst": analyst}

BUDGET = float(os.getenv("BUDGET_MENSILE", 500))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        req.post(url, json={"chat_id": TELEGRAM_CHAT, "text": message})
    except:
        pass

def check_budget(costo):
    budget_usato = float(os.getenv("BUDGET_USATO", 0))
    budget_rimasto = BUDGET - budget_usato
    if costo > budget_rimasto:
        send_telegram(f"Budget insufficiente! Richiesto: {costo}, Disponibile: {budget_rimasto}")
        return False
    if costo > 50:
        send_telegram(f"Richiesta spesa: {costo}. Budget: {budget_rimasto}. Rispondi SI o NO")
        return False
    nuovo_usato = budget_usato + costo
    os.environ["BUDGET_USATO"] = str(nuovo_usato)
    send_telegram(f"Spesa approvata: {costo}. Budget rimasto: {BUDGET - nuovo_usato}")
    return True

def ceo_think():
    markets = db.table("markets").select("*").eq("active", True).execute()
    clients = db.table("clients").select("*").eq("status", "active").execute()
    prospects = db.table("prospects").select("*").eq("status", "new").limit(20).execute()
    
    context = {
        "time": datetime.now().isoformat(),
        "active_clients": len(clients.data) if clients.data else 0,
        "mrr": sum(c["mrr"] for c in clients.data) if clients.data else 0,
        "new_prospects": len(prospects.data) if prospects.data else 0,
        "budget_remaining": BUDGET - float(os.getenv("BUDGET_USATO", 0)),
        "markets": markets.data
    }
    
    prompt = "Sei il CEO di una agenzia AI. Devi massimizzare fatturato. Stato: " + json.dumps(context) + " Agenti: Hunter, Closer, Delivery, Analyst. Rispondi SOLO JSON con: priority, agent, action, params, costo_stimato, reasoning."
    
    response = claude.messages.create(
        model="claude-opus-4-8",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    
    try:
        decision = json.loads(response.content[0].text)
    except:
        decision = {"priority": "ACQUISIRE", "agent": "Hunter", "action": "cerca prospect", "params": {"sector": "immobiliare", "location": "Milano"}, "costo_stimato": 0, "reasoning": "fallback"}
    
    costo = decision.get("costo_stimato", 0)
    if costo > 0:
        if not check_budget(costo):
            decision["blocked"] = "budget"
    
    try:
        db.table("decisions").insert({
            "agent_name": "CEO",
            "thought_process": decision.get("reasoning"),
            "decision": decision.get("priority"),
            "action_taken": decision.get("action"),
            "result": json.dumps(decision.get("params", {}))
        }).execute()
    except:
        pass
    
    return decision

def agency_loop():
    send_telegram("AGENZIA AI AVVIATA - Budget: 500 EUR")
    print("AGENZIA AI AVVIATA")
    iteration = 0
    while True:
        iteration += 1
        try:
            decision = ceo_think()
            msg = f"Ciclo {iteration} - CEO: {decision.get('priority')} - Agente: {decision.get('agent')}"
            if decision.get("blocked"):
                msg += " - BLOCCATO"
            else:
                agent_name = decision.get("agent")
                if agent_name and agent_name in AGENTS:
                    agent = AGENTS[agent_name]
                    agent_decision = agent.think({"ceo_decision": decision})
                    if agent_decision.get("tool"):
                        result = agent.execute(agent_decision["tool"], agent_decision.get("params", {}))
                        msg += f" - Fatto: {json.dumps(result, ensure_ascii=False)[:150]}"
            send_telegram(msg)
            print(msg)
        except Exception as e:
            send_telegram(f"ERRORE: {str(e)[:200]}")
            print(f"ERRORE: {e}")
        time.sleep(7200)

@app.route("/")
def dashboard():
    return "<html><head><title>GetAutomatik AI</title><meta charset='UTF-8'><style>body{font-family:Arial;background:#0a0a0a;color:white;padding:30px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:20px}.card{background:#1a1a1a;padding:25px;border-radius:12px}.number{font-size:48px;font-weight:bold;color:#00ff88}.label{color:#888}</style></head><body><h1>GetAutomatik AI</h1><div class='grid'><div class='card'><div class='number' id='clients'>0</div><div class='label'>Clienti</div></div><div class='card'><div class='number' id='mrr'>0</div><div class='label'>MRR</div></div><div class='card'><div class='number' id='budget'>500</div><div class='label'>Budget</div></div><div class='card'><div class='number' id='markets'>5</div><div class='label'>Mercati</div></div></div><script>setInterval(async()=>{const r=await fetch('/api/status');const d=await r.json();document.getElementById('clients').textContent=d.clients;document.getElementById('mrr').textContent=d.mrr;document.getElementById('budget').textContent=d.budget;document.getElementById('markets').textContent=d.markets},5000)</script></body></html>"

@app.route("/api/status")
def status():
    clients = db.table("clients").select("*").eq("status", "active").execute()
    prospects = db.table("prospects").select("*").execute()
    markets = db.table("markets").select("*").eq("active", True).execute()
    return jsonify({"clients": len(clients.data) if clients.data else 0, "mrr": sum(c["mrr"] for c in clients.data) if clients.data else 0, "prospects": len(prospects.data) if prospects.data else 0, "markets": len(markets.data) if markets.data else 0, "budget": BUDGET - float(os.getenv("BUDGET_USATO", 0))})

if __name__ == "__main__":
    try:
        db.table("metrics").insert({"date": datetime.now().date().isoformat()}).execute()
    except:
        pass
    threading.Thread(target=agency_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)