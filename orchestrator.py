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
    decisions = db.table("decisions").select("*").order("created_at", desc=True).limit(10).execute()
    
    # Analizza performance mercati
    market_data = []
    for m in (markets.data or []):
        sector = m["sector"]
        m_prospects = db.table("prospects").select("*").eq("sector", sector).execute()
        m_clients = db.table("clients").select("*").eq("sector", sector).execute()
        total_p = len(m_prospects.data) if m_prospects.data else 0
        total_c = len(m_clients.data) if m_clients.data else 0
        conversion = (total_c / total_p * 100) if total_p > 0 else 0
        score = m.get("score", 50)
        # Se conversione < 2% dopo 20+ prospect, riduci score
        if total_p >= 20 and conversion < 2:
            score = max(10, score - 20)
            db.table("markets").update({"score": score}).eq("sector", sector).execute()
        market_data.append({
            "sector": sector, "score": score, "prospects": total_p,
            "clients": total_c, "conversion": round(conversion, 1)
        })
    
    # Trova mercato migliore e peggiore
    best_market = max(market_data, key=lambda x: x["score"]) if market_data else None
    worst_market = min(market_data, key=lambda x: x["score"]) if market_data else None
    
    context = {
        "time": datetime.now().isoformat(),
        "active_clients": len(clients.data) if clients.data else 0,
        "mrr": sum(c["mrr"] for c in clients.data) if clients.data else 0,
        "new_prospects": len(prospects.data) if prospects.data else 0,
        "budget_remaining": BUDGET - float(os.getenv("BUDGET_USATO", 0)),
        "markets": market_data,
        "best_market": best_market["sector"] if best_market else None,
        "worst_market": worst_market["sector"] if worst_market else None,
        "recent_decisions": [d.get("decision") for d in (decisions.data or [])[:5]]
    }
    
    # Se il mercato peggiore ha score < 20, suggerisci switch
    switch_hint = ""
    if worst_market and worst_market["score"] < 20 and best_market and best_market["score"] > 50:
        switch_hint = f" Il mercato {worst_market['sector']} performa male. Considera di spostarti su {best_market['sector']} o esplorare nuovi mercati."
    
    prompt = "Sei il CEO di una agenzia AI. Devi massimizzare fatturato." + switch_hint + " Stato: " + json.dumps(context) + " Agenti: Hunter, Closer, Delivery, Analyst. Puoi cambiare mercato con params: {sector: 'nuovo_mercato'}. Rispondi SOLO JSON con: priority, agent, action, params, costo_stimato, reasoning."
    
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

@app.route("/landing")
def landing():
    return open("templates/landing.html", "r").read()

@app.route("/")
def dashboard():
    return """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GetAutomatik AI - Command Center</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700;900&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background: #050510; color: #fff; min-height: 100vh; }
        .bg-grid { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background-image: radial-gradient(circle at 1px 1px, rgba(255,255,255,0.03) 1px, transparent 0); background-size: 40px 40px; pointer-events: none; z-index: 0; }
        .container { max-width: 1400px; margin: 0 auto; padding: 30px; position: relative; z-index: 1; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 40px; padding-bottom: 20px; border-bottom: 1px solid rgba(255,255,255,0.06); }
        .logo { display: flex; align-items: center; gap: 15px; }
        .logo-icon { width: 45px; height: 45px; background: linear-gradient(135deg, #00ff88, #00b4d8); border-radius: 12px; display: flex; align-items: center; justify-content: center; font-size: 22px; }
        .logo h1 { font-size: 24px; font-weight: 700; letter-spacing: -0.5px; }
        .logo span { color: #00ff88; }
        .status-badge { background: rgba(0,255,136,0.1); border: 1px solid rgba(0,255,136,0.3); padding: 8px 18px; border-radius: 50px; font-size: 13px; color: #00ff88; display: flex; align-items: center; gap: 8px; }
        .pulse { width: 8px; height: 8px; background: #00ff88; border-radius: 50%; animation: pulse 2s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
        .metrics-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; margin-bottom: 40px; }
        .metric-card { background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.06); border-radius: 16px; padding: 25px; position: relative; overflow: hidden; transition: all 0.3s; }
        .metric-card:hover { border-color: rgba(0,255,136,0.3); transform: translateY(-2px); }
        .metric-card .icon { font-size: 28px; margin-bottom: 15px; }
        .metric-card .value { font-size: 42px; font-weight: 900; letter-spacing: -2px; margin-bottom: 5px; }
        .metric-card .label { color: rgba(255,255,255,0.5); font-size: 13px; text-transform: uppercase; letter-spacing: 1px; }
        .metric-card.accent-green .value { color: #00ff88; }
        .metric-card.accent-blue .value { color: #00b4d8; }
        .metric-card.accent-purple .value { color: #7b2ff7; }
        .metric-card.accent-orange .value { color: #ff6b35; }
        .section-title { font-size: 16px; font-weight: 600; text-transform: uppercase; letter-spacing: 2px; color: rgba(255,255,255,0.4); margin-bottom: 20px; }
        .agents-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 40px; }
        .agent-card { background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.06); border-radius: 16px; padding: 20px; text-align: center; transition: all 0.3s; }
        .agent-card:hover { border-color: rgba(255,255,255,0.15); }
        .agent-avatar { width: 60px; height: 60px; border-radius: 50%; margin: 0 auto 15px; display: flex; align-items: center; justify-content: center; font-size: 28px; }
        .agent-card h3 { font-size: 15px; font-weight: 600; margin-bottom: 5px; }
        .agent-card .role { font-size: 11px; color: rgba(255,255,255,0.4); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }
        .agent-card .status-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 5px; }
        .agent-card .status-dot.active { background: #00ff88; }
        .agent-card .status-dot.thinking { background: #ffaa00; animation: pulse 1s infinite; }
        .agent-card .last-action { font-size: 11px; color: rgba(255,255,255,0.3); margin-top: 8px; }
        .ceo { background: linear-gradient(135deg, #1a1a2e, #16213e); border-color: rgba(0,180,216,0.3); }
        .hunter { background: linear-gradient(135deg, #1a1a2e, #1a1a2e); }
        .closer { background: linear-gradient(135deg, #1a1a2e, #1a1a2e); }
        .delivery { background: linear-gradient(135deg, #1a1a2e, #1a1a2e); }
        .analyst { background: linear-gradient(135deg, #1a1a2e, #1a1a2e); }
        .log-section { background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.06); border-radius: 16px; padding: 25px; }
        .log-entry { padding: 12px 0; border-bottom: 1px solid rgba(255,255,255,0.03); font-size: 13px; display: flex; align-items: center; gap: 12px; color: rgba(255,255,255,0.6); }
        .log-entry:last-child { border-bottom: none; }
        .log-time { color: rgba(255,255,255,0.2); font-size: 11px; min-width: 50px; }
        .log-agent { font-weight: 600; min-width: 80px; }
        .log-agent.ceo-tag { color: #00b4d8; }
        .log-agent.hunter-tag { color: #7b2ff7; }
        .log-agent.closer-tag { color: #ff6b35; }
        .log-agent.delivery-tag { color: #00ff88; }
        .log-agent.analyst-tag { color: #ffaa00; }
        .footer { text-align: center; padding: 30px; color: rgba(255,255,255,0.15); font-size: 12px; }
        @media (max-width: 900px) {
            .metrics-grid, .agents-grid { grid-template-columns: repeat(2, 1fr); }
        }
    </style>
</head>
<body>
    <div class="bg-grid"></div>
    <div class="container">
        <div class="header">
            <div class="logo">
                <div class="logo-icon">G</div>
                <h1>GetAutomatik <span>AI</span></h1>
            </div>
            <div class="status-badge"><div class="pulse"></div>Sistema Attivo</div>
        </div>
        
        <div class="metrics-grid">
            <div class="metric-card accent-green">
                <div class="icon">👥</div>
                <div class="value" id="clients">0</div>
                <div class="label">Clienti Attivi</div>
            </div>
            <div class="metric-card accent-blue">
                <div class="icon">💰</div>
                <div class="value" id="mrr">0€</div>
                <div class="label">MRR Mensile</div>
            </div>
            <div class="metric-card accent-purple">
                <div class="icon">🎯</div>
                <div class="value" id="prospects">0</div>
                <div class="label">Prospect Totali</div>
            </div>
            <div class="metric-card accent-orange">
                <div class="icon">🌍</div>
                <div class="value" id="markets">5</div>
                <div class="label">Mercati Attivi</div>
            </div>
        </div>
        
        <div class="section-title">🧠 Team Agenti AI</div>
        <div class="agents-grid" id="agents-grid">
            <div class="agent-card ceo">
                <div class="agent-avatar" style="background: linear-gradient(135deg, #00b4d8, #0077b6);">🧠</div>
                <h3>CEO</h3>
                <div class="role">Strategia & Decisioni</div>
                <div><span class="status-dot thinking"></span>Thinking...</div>
                <div class="last-action">In attesa...</div>
            </div>
            <div class="agent-card hunter">
                <div class="agent-avatar" style="background: linear-gradient(135deg, #7b2ff7, #5a189a);">🎯</div>
                <h3>Hunter</h3>
                <div class="role">Acquisizione</div>
                <div><span class="status-dot active"></span>Idle</div>
                <div class="last-action">In attesa...</div>
            </div>
            <div class="agent-card closer">
                <div class="agent-avatar" style="background: linear-gradient(135deg, #ff6b35, #e63946);">💼</div>
                <h3>Closer</h3>
                <div class="role">Vendite</div>
                <div><span class="status-dot active"></span>Idle</div>
                <div class="last-action">In attesa...</div>
            </div>
            <div class="agent-card delivery">
                <div class="agent-avatar" style="background: linear-gradient(135deg, #00ff88, #00b4d8);">⚡</div>
                <h3>Delivery</h3>
                <div class="role">Operations</div>
                <div><span class="status-dot active"></span>Idle</div>
                <div class="last-action">In attesa...</div>
            </div>
            <div class="agent-card analyst" style="grid-column: span 2;">
                <div class="agent-avatar" style="background: linear-gradient(135deg, #ffaa00, #ff6b35);">📊</div>
                <h3>Analyst</h3>
                <div class="role">Data & Ottimizzazione</div>
                <div><span class="status-dot active"></span>Idle</div>
                <div class="last-action">In attesa...</div>
            </div>
        </div>
        
        <div class="log-section">
            <div class="section-title">📋 Activity Log</div>
            <div id="log-container">
                <div class="log-entry"><span class="log-time">--:--</span><span class="log-agent">Sistema</span>In attesa del primo ciclo...</div>
            </div>
        </div>
        
        <div class="footer">GetAutomatik AI © 2026 | Fully Autonomous Agency | Budget: <span id="budget-footer">500€</span></div>
    </div>
    
    <script>
        async function updateDashboard() {
            try {
                const resp = await fetch('/api/status');
                const data = await resp.json();
                document.getElementById('clients').textContent = data.clients;
                document.getElementById('mrr').textContent = data.mrr + '€';
                document.getElementById('prospects').textContent = data.prospects;
                document.getElementById('markets').textContent = data.markets;
                document.getElementById('budget-footer').textContent = data.budget + '€';
            } catch(e) {}
        }
        
        async function updateLogs() {
            try {
                const resp = await fetch('/api/logs');
                const logs = await resp.json();
                const container = document.getElementById('log-container');
                container.innerHTML = logs.map(l => 
                    `<div class="log-entry">
                        <span class="log-time">${l.time}</span>
                        <span class="log-agent ${l.agent}-tag">${l.agent}</span>
                        ${l.message}
                    </div>`
                ).join('');
            } catch(e) {}
        }
        
        setInterval(updateDashboard, 5000);
        setInterval(updateLogs, 10000);
        updateDashboard();
        updateLogs();
    </script>
</body>
</html>"""


@app.route("/api/status")
def status():
    clients = db.table("clients").select("*").eq("status", "active").execute()
    prospects = db.table("prospects").select("*").execute()
    markets = db.table("markets").select("*").eq("active", True).execute()
    return jsonify({"clients": len(clients.data) if clients.data else 0, "mrr": sum(c["mrr"] for c in clients.data) if clients.data else 0, "prospects": len(prospects.data) if prospects.data else 0, "markets": len(markets.data) if markets.data else 0, "budget": BUDGET - float(os.getenv("BUDGET_USATO", 0))})


@app.route("/api/logs")
def logs():
    try:
        decisions = db.table("decisions").select("*").order("created_at", desc=True).limit(20).execute()
        result = []
        for d in (decisions.data or []):
            result.append({
                "time": d["created_at"][11:16] if d.get("created_at") else "--:--",
                "agent": d.get("agent_name", "Sistema"),
                "message": (d.get("decision") or "") + " → " + (d.get("action_taken") or "")
            })
        return jsonify(result)
    except:
        return jsonify([])


@app.route("/webhook/signup", methods=["POST"])
def signup():
    data = request.json
    company = data.get("company", "")
    email = data.get("email", "")
    sector = data.get("sector", "altro")
    whatsapp = data.get("whatsapp", "")
    
    if company and email:
        try:
            db.table("clients").insert({
                "company_name": company,
                "contact_email": email,
                "sector": sector,
                "plan": "monthly",
                "mrr": 197,
                "status": "payment_pending"
            }).execute()
            send_telegram(f"🎉 Nuovo cliente! {company} ({email}) - Settore: {sector}")
        except Exception as e:
            print(f"Errore signup: {e}")
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "Dati mancanti"}), 400

if __name__ == "__main__":
    try:
        db.table("metrics").insert({"date": datetime.now().date().isoformat()}).execute()
    except:
        pass
    threading.Thread(target=agency_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)