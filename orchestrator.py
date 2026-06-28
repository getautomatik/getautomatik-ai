from flask import Flask, jsonify, request, redirect, render_template
from agents import create_hunter, create_closer, create_delivery, create_analyst, check_email_replies, send_followups, ceo_optimize_emails
from supabase import create_client
from dotenv import load_dotenv
import anthropic
import os
import json
import threading
import time
import hmac as hmac_mod
import hashlib
import base64
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

DEFAULT_NICHES = ["immobiliare", "dentisti", "palestre", "ristoranti", "studi legali"]

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        req.post(url, json={"chat_id": TELEGRAM_CHAT, "text": message})
    except:
        pass

def send_email(to_email, subject, body_html):
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    email_from = os.getenv("EMAIL_ADDRESS")
    email_pass = os.getenv("EMAIL_PASSWORD")
    if not email_from or not email_pass:
        print("Email SMTP non configurata (EMAIL_ADDRESS / EMAIL_PASSWORD)")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = email_from
        msg["To"] = to_email
        msg.attach(MIMEText(body_html, "html"))
        with smtplib.SMTP_SSL("smtp.zoho.eu", 465) as server:
            server.login(email_from, email_pass)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Errore invio email: {e}")
        return False

def generate_onboarding_token(email):
    secret = os.getenv("SECRET_KEY", "getautomatik-secret-key")
    sig = hmac_mod.new(secret.encode(), email.encode(), hashlib.sha256).hexdigest()[:20]
    email_b64 = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    return f"{email_b64}.{sig}"

def decode_onboarding_token(token):
    try:
        email_b64, sig = token.rsplit(".", 1)
        padding = 4 - len(email_b64) % 4
        email = base64.urlsafe_b64decode(email_b64 + "=" * padding).decode()
        expected = generate_onboarding_token(email).rsplit(".", 1)[1]
        if hmac_mod.compare_digest(sig, expected):
            return email
    except Exception:
        pass
    return None

def send_onboarding_email(to_email, name):
    token = generate_onboarding_token(to_email)
    onboarding_url = f"https://getautomatik.com/onboarding?token={token}"
    subject = "Benvenuto in GetAutomatik — iniziamo subito"
    body = f"""
    <div style="font-family:Inter,sans-serif;background:#050510;color:#fff;padding:48px 32px;max-width:560px;margin:0 auto;border-radius:16px;">
        <div style="font-size:15px;font-weight:700;color:#00ff88;margin-bottom:28px;">GetAutomatik AI</div>
        <h1 style="font-size:26px;font-weight:800;letter-spacing:-1px;margin-bottom:12px;">Benvenuto, {name}! 🚀</h1>
        <p style="font-size:15px;color:rgba(255,255,255,0.6);line-height:1.7;margin-bottom:24px;">
            Il tuo trial di 7 giorni è attivo. Per attivare l'agente AI completamente dobbiamo configurarlo per la tua agenzia.
        </p>
        <p style="font-size:15px;color:rgba(255,255,255,0.6);line-height:1.7;margin-bottom:32px;">
            Ci vogliono meno di 5 minuti. Compila il form e il tuo agente sarà operativo entro 24 ore lavorative.
        </p>
        <a href="{onboarding_url}" style="display:inline-block;background:linear-gradient(135deg,#00ff88,#00b4d8);color:#050510;padding:16px 36px;border-radius:10px;font-size:15px;font-weight:800;text-decoration:none;">
            Configura il mio agente →
        </a>
        <p style="font-size:12px;color:rgba(255,255,255,0.2);margin-top:32px;">
            Hai domande? Rispondi a questa email o scrivi a info@getautomatik.com
        </p>
    </div>
    """
    return send_email(to_email, subject, body)

def _get_budget_usato():
    try:
        s = db.table("settings").select("value").eq("key", "budget_usato").execute()
        return float(s.data[0]["value"]) if s.data else 0.0
    except Exception:
        return float(os.getenv("BUDGET_USATO", 0))

def _set_budget_usato(value):
    try:
        existing = db.table("settings").select("id").eq("key", "budget_usato").execute()
        if existing.data:
            db.table("settings").update({"value": str(round(value, 4))}).eq("key", "budget_usato").execute()
        else:
            db.table("settings").insert({"key": "budget_usato", "value": str(round(value, 4))}).execute()
    except Exception:
        os.environ["BUDGET_USATO"] = str(value)

def check_budget(costo):
    budget_usato = _get_budget_usato()
    budget_rimasto = BUDGET - budget_usato
    if costo > budget_rimasto:
        send_telegram(f"Budget insufficiente! Richiesto: {costo}, Disponibile: {budget_rimasto:.2f}")
        return False
    if costo > 50:
        send_telegram(f"Richiesta spesa: {costo}. Budget: {budget_rimasto:.2f}. Rispondi SI o NO")
        return False
    nuovo_usato = budget_usato + costo
    _set_budget_usato(nuovo_usato)
    send_telegram(f"Spesa approvata: {costo}. Budget rimasto: {BUDGET - nuovo_usato:.2f}")
    return True

def _compute_analytics(db):
    """Real-time KPIs for CEO decision making."""
    try:
        all_p = db.table("prospects").select("sector,status,follow_up_at").execute()
        sectors = {}
        warm_count = 0
        for p in (all_p.data or []):
            s = p.get("sector", "altro")
            st = p.get("status", "new")
            if s not in sectors:
                sectors[s] = {"total": 0, "contacted": 0, "replied": 0, "warm": 0, "converted": 0}
            sectors[s]["total"] += 1
            if st in ("contacted", "followup_1", "replied", "warm_1", "warm_2", "warm_closed", "converted", "dead"):
                sectors[s]["contacted"] += 1
            if st in ("replied", "warm_1", "warm_2", "warm_closed", "converted"):
                sectors[s]["replied"] += 1
            if st in ("warm_1", "warm_2"):
                sectors[s]["warm"] += 1
                warm_count += 1
            if st == "converted":
                sectors[s]["converted"] += 1

        reply_rates = {}
        for s, v in sectors.items():
            if v["contacted"] >= 5:
                reply_rates[s] = round(v["replied"] / v["contacted"] * 100, 1)

        best_sector = max(reply_rates, key=reply_rates.get) if reply_rates else None
        worst_sector = min(reply_rates, key=reply_rates.get) if len(reply_rates) > 1 else None

        spent = _get_budget_usato()
        clients = db.table("clients").select("mrr").eq("status", "active").execute()
        mrr = sum(c.get("mrr", 0) for c in (clients.data or []))
        total_replied = sum(v["replied"] for v in sectors.values())
        total_contacted = sum(v["contacted"] for v in sectors.values())
        cost_per_reply = round(spent / total_replied, 2) if total_replied > 0 else 0
        cost_per_client = round(spent / len(clients.data), 2) if clients.data else 0

        return {
            "mrr": mrr,
            "spent": round(spent, 2),
            "profit": round(mrr - spent, 2),
            "warm_prospects": warm_count,
            "reply_rate_by_sector": reply_rates,
            "best_sector": best_sector,
            "worst_sector": worst_sector,
            "cost_per_reply": cost_per_reply,
            "cost_per_client": cost_per_client,
            "sector_detail": {s: sectors[s] for s in list(sectors.keys())[:6]}
        }
    except Exception as e:
        print(f"Analytics error: {e}")
        return {}


def ceo_think():
    # Seed nicchie di default se la tabella markets è vuota
    try:
        existing_markets = db.table("markets").select("sector").execute()
        existing_sectors = {m["sector"] for m in (existing_markets.data or [])}
        for niche in DEFAULT_NICHES:
            if niche not in existing_sectors:
                db.table("markets").insert({"sector": niche, "score": 50, "leads_found": 0, "active": True}).execute()
    except Exception as e:
        print(f"Seed nicchie: {e}")

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
    
    analytics = _compute_analytics(db)
    niches_available = ", ".join(DEFAULT_NICHES)
    prompt = (
        f"Sei il CEO di GetAutomatik AI, agenzia che vende agenti AI a PMI italiane (197€/mese).\n"
        f"Obiettivo: massimizzare MRR e chiudere contratti.\n\n"
        f"ANALYTICS REALI:\n{json.dumps(analytics, ensure_ascii=False)}\n\n"
        f"STATO OPERATIVO:\n{json.dumps(context, ensure_ascii=False)}\n\n"
        f"REGOLE:\n"
        f"- Se ci sono prospect warm (warm_prospects > 0), dai ALTA priorità a Closer per inviargli email\n"
        f"- Attacca il settore con reply rate più alto ({analytics.get('best_sector', 'sconosciuto')})\n"
        f"- Evita settori con reply rate < 2% se hai alternative\n"
        f"- Hunter cerca nuovi prospect, Closer li contatta, Delivery serve clienti, Analyst ottimizza\n"
        f"- Nicchie disponibili: {niches_available}\n"
        f"- Puoi cambiare mercato con params: {{\"sector\": \"nuova_nicchia\"}}\n\n"
        f"{switch_hint}\n\n"
        f"Rispondi SOLO JSON: {{\"priority\": str, \"agent\": str, \"action\": str, \"params\": dict, \"costo_stimato\": float, \"reasoning\": str}}"
    )
    
    response = claude.messages.create(
        model="claude-opus-4-8",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    
    try:
        decision = json.loads(response.content[0].text)
    except:
        import random
        fallback_sector = random.choice(DEFAULT_NICHES)
        decision = {"priority": "ACQUISIRE", "agent": "Hunter", "action": "cerca prospect", "params": {"sector": fallback_sector, "location": "Milano"}, "costo_stimato": 0, "reasoning": "fallback"}
    
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

def imap_loop():
    time.sleep(120)
    while True:
        try:
            n = check_email_replies(db)
            if n:
                print(f"IMAP: {n} nuove risposte")
        except Exception as e:
            print(f"IMAP loop error: {e}")
        time.sleep(1800)

def agency_loop():
    send_telegram("AGENZIA AI AVVIATA - Budget: 500 EUR")
    print("AGENZIA AI AVVIATA")
    iteration = 0
    while True:
        iteration += 1
        try:
            # 1. Invia follow-up scaduti (every cycle, query returns only due ones)
            try:
                followups_sent = send_followups(db)
                if followups_sent:
                    send_telegram(f"📤 Follow-up inviati: {followups_sent}")
            except Exception as e:
                print(f"Follow-up failed: {e}")

            # 3. CEO pensa e delega all'agente
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

            # 4. CEO ottimizza prompt email ogni 5 cicli (ogni ~10h)
            if iteration % 5 == 0:
                try:
                    ceo_optimize_emails(db)
                except Exception as e:
                    print(f"CEO optimize failed: {e}")

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
        .pipeline-section { background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.06); border-radius: 16px; padding: 25px; margin-bottom: 30px; }
        .pipeline-table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        .pipeline-table th { text-align: left; padding: 10px 14px; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: rgba(255,255,255,0.3); border-bottom: 1px solid rgba(255,255,255,0.06); }
        .pipeline-table td { padding: 12px 14px; font-size: 14px; border-bottom: 1px solid rgba(255,255,255,0.03); }
        .pipeline-table tr:last-child td { border-bottom: none; }
        .pipeline-table .sector-name { font-weight: 600; color: #fff; text-transform: capitalize; }
        .bar-bg { background: rgba(255,255,255,0.06); border-radius: 4px; height: 6px; min-width: 80px; }
        .bar-fill { height: 6px; border-radius: 4px; background: linear-gradient(90deg, #00ff88, #00b4d8); transition: width 0.6s; }
        .conv-rate { color: #00ff88; font-weight: 700; }
        .empty-pipeline { color: rgba(255,255,255,0.2); font-size: 13px; text-align: center; padding: 30px 0; }
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
            <div class="metric-card" style="border-color:rgba(255,170,0,0.3);grid-column:span 4;">
                <div style="display:flex;align-items:center;gap:20px;">
                    <div style="font-size:28px;">🔥</div>
                    <div>
                        <div style="font-size:32px;font-weight:900;color:#ffaa00;" id="warm-count">0</div>
                        <div style="font-size:13px;color:rgba(255,255,255,0.4);text-transform:uppercase;letter-spacing:1px;">Prospect Caldi in Closing Sequence</div>
                    </div>
                    <div style="margin-left:auto;font-size:13px;color:rgba(255,255,255,0.3);">warm_1 / warm_2 / call_scheduled</div>
                </div>
            </div>
        </div>

        <div class="metrics-grid" style="margin-bottom:30px;">
            <div class="metric-card" style="border-color:rgba(0,255,136,0.2);">
                <div class="icon">📊</div>
                <div class="value" style="color:#00ff88;font-size:32px;" id="pl-profit">0€</div>
                <div class="label">Profitto Netto</div>
            </div>
            <div class="metric-card">
                <div class="icon">📧</div>
                <div class="value" style="font-size:32px;" id="pl-emails">0</div>
                <div class="label">Email Inviate</div>
            </div>
            <div class="metric-card">
                <div class="icon">💬</div>
                <div class="value" style="color:#00b4d8;font-size:32px;" id="pl-rate">0%</div>
                <div class="label">Reply Rate</div>
            </div>
            <div class="metric-card">
                <div class="icon">💸</div>
                <div class="value" style="color:#ff6b35;font-size:32px;" id="pl-spent">0€</div>
                <div class="label">Costi Operativi</div>
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
        
        <div class="pipeline-section" style="padding:16px 25px;margin-bottom:20px;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                <span id="daily-label" style="font-size:13px;color:rgba(255,255,255,0.5);">Email oggi: 0/30</span>
                <span style="font-size:12px;color:rgba(255,255,255,0.25);">Limite giornaliero per deliverability</span>
            </div>
            <div style="background:rgba(255,255,255,0.06);border-radius:6px;height:8px;">
                <div id="daily-bar" style="height:8px;border-radius:6px;width:0%;transition:width 0.6s;background:linear-gradient(90deg,#00ff88,#00b4d8);"></div>
            </div>
        </div>

        <div class="pipeline-section">
            <div class="section-title">🎯 Prospect Pipeline</div>
            <div id="pipeline-container"><div class="empty-pipeline">Caricamento...</div></div>
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
                if (data.warm !== undefined) document.getElementById('warm-count').textContent = data.warm;
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
        
        async function updatePL() {
            try {
                const resp = await fetch('/api/pl');
                const d = await resp.json();
                document.getElementById('pl-profit').textContent = d.profit + '€';
                document.getElementById('pl-emails').textContent = d.emails_sent;
                document.getElementById('pl-rate').textContent = d.reply_rate + '%';
                document.getElementById('pl-spent').textContent = d.spent + '€';
                const pct = Math.min(100, Math.round((d.daily_sent || 0) / (d.daily_limit || 30) * 100));
                const bar = document.getElementById('daily-bar');
                if (bar) {
                    bar.style.width = pct + '%';
                    bar.style.background = pct > 80 ? '#ff6b35' : 'linear-gradient(90deg,#00ff88,#00b4d8)';
                }
                const lbl = document.getElementById('daily-label');
                if (lbl) lbl.textContent = `Email oggi: ${d.daily_sent||0}/${d.daily_limit||30}`;
            } catch(e) {}
        }

        async function updatePipeline() {
            try {
                const resp = await fetch('/prospects');
                const data = await resp.json();
                const container = document.getElementById('pipeline-container');
                if (!data.length) {
                    container.innerHTML = '<div class="empty-pipeline">Nessun prospect trovato ancora. L\'agente Hunter sta cercando...</div>';
                    return;
                }
                const maxTotal = Math.max(...data.map(d => d.total), 1);
                container.innerHTML = '<table class="pipeline-table"><thead><tr><th>Nicchia</th><th>Totale</th><th>Contattati</th><th>Risposto</th><th>Conversion</th><th>Progresso</th></tr></thead><tbody>' +
                    data.map(d => {
                        const pct = Math.round(d.total / maxTotal * 100);
                        return `<tr>
                            <td class="sector-name">${d.sector}</td>
                            <td>${d.total}</td>
                            <td>${d.contacted}</td>
                            <td>${d.replied}</td>
                            <td class="conv-rate">${d.conversion_rate}%</td>
                            <td><div class="bar-bg"><div class="bar-fill" style="width:${pct}%"></div></div></td>
                        </tr>`;
                    }).join('') + '</tbody></table>';
            } catch(e) {}
        }

        setInterval(updateDashboard, 5000);
        setInterval(updateLogs, 10000);
        setInterval(updatePipeline, 30000);
        setInterval(updatePL, 60000);
        updateDashboard();
        updateLogs();
        updatePipeline();
        updatePL();
    </script>
</body>
</html>"""


@app.route("/api/status")
def status():
    clients = db.table("clients").select("*").eq("status", "active").execute()
    prospects = db.table("prospects").select("*").execute()
    markets = db.table("markets").select("*").eq("active", True).execute()
    warm = db.table("prospects").select("id").in_("status", ["warm_1", "warm_2", "call_scheduled"]).execute()
    budget_usato = _get_budget_usato()
    return jsonify({
        "clients": len(clients.data) if clients.data else 0,
        "mrr": sum(c["mrr"] for c in clients.data) if clients.data else 0,
        "prospects": len(prospects.data) if prospects.data else 0,
        "markets": len(markets.data) if markets.data else 0,
        "budget": round(BUDGET - budget_usato, 2),
        "warm": len(warm.data) if warm.data else 0
    })


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


@app.route("/prospects")
def prospects_pipeline():
    try:
        rows = db.table("prospects").select("sector,status").execute()
        pipeline = {}
        for p in (rows.data or []):
            s = p.get("sector", "altro")
            if s not in pipeline:
                pipeline[s] = {"total": 0, "contacted": 0, "replied": 0}
            pipeline[s]["total"] += 1
            if p.get("status") in ("contacted", "followup_1", "replied", "warm_1", "warm_2",
                                      "warm_closed", "call_scheduled", "converted", "dead"):
                pipeline[s]["contacted"] += 1
            if p.get("status") in ("replied", "warm_1", "warm_2", "warm_closed",
                                   "call_scheduled", "converted"):
                pipeline[s]["replied"] += 1
        result = []
        for sector, stats in pipeline.items():
            conv = round(stats["replied"] / stats["contacted"] * 100, 1) if stats["contacted"] > 0 else 0
            result.append({"sector": sector, "total": stats["total"], "contacted": stats["contacted"], "replied": stats["replied"], "conversion_rate": conv})
        result.sort(key=lambda x: x["total"], reverse=True)
        return jsonify(result)
    except Exception:
        return jsonify([])


@app.route("/api/pl")
def pl_report():
    try:
        clients = db.table("clients").select("mrr").eq("status", "active").execute()
        mrr = sum(c.get("mrr", 0) for c in (clients.data or []))
        total_emails = db.table("prospects").select("id").in_("status", ["contacted", "followup_1", "followup_2", "replied", "converted"]).execute()
        replied = db.table("prospects").select("id").in_("status", ["replied", "converted"]).execute()
        total_contacted = len(total_emails.data) if total_emails.data else 0
        total_replied = len(replied.data) if replied.data else 0
        reply_rate = round(total_replied / total_contacted * 100, 1) if total_contacted > 0 else 0
        try:
            s = db.table("settings").select("value").eq("key", "budget_usato").execute()
            spent = round(float(s.data[0]["value"]), 2) if s.data else 0.0
        except Exception:
            spent = 0.0
        try:
            today_key = f"daily_emails_{datetime.now().date().isoformat()}"
            de = db.table("settings").select("value").eq("key", today_key).execute()
            daily_sent = int(de.data[0]["value"]) if de.data else 0
        except Exception:
            daily_sent = 0
        return jsonify({
            "mrr": mrr,
            "spent": spent,
            "profit": round(mrr - spent, 2),
            "emails_sent": total_contacted,
            "replies": total_replied,
            "reply_rate": reply_rate,
            "daily_sent": daily_sent,
            "daily_limit": 30
        })
    except Exception as e:
        return jsonify({"mrr": 0, "spent": 0, "profit": 0, "emails_sent": 0, "replies": 0, "reply_rate": 0})

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


@app.route("/checkout")
def checkout():
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "product_data": {"name": "GetAutomatik AI"},
                    "unit_amount": 19700,
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            mode="subscription",
            subscription_data={"trial_period_days": 7},
            success_url="https://getautomatik.com/success",
            cancel_url="https://getautomatik.com/landing",
        )
        return redirect(session.url, code=302)
    except Exception as e:
        return f"Errore Stripe: {str(e)}"

@app.route("/success")
def success():
    return """<html><body style='background:#050510;color:white;font-family:Inter,sans-serif;text-align:center;padding:80px 20px;'>
    <div style='max-width:480px;margin:0 auto;'>
        <div style='font-size:54px;margin-bottom:20px;'>🚀</div>
        <h1 style='color:#00ff88;font-size:30px;letter-spacing:-1px;margin-bottom:14px;'>Trial attivato!</h1>
        <p style='color:rgba(255,255,255,0.55);font-size:16px;line-height:1.7;margin-bottom:28px;'>
            Perfetto! Riceverai a breve una email con il link per configurare il tuo agente.<br>
            <strong style='color:white;'>Controlla la casella di posta (anche lo spam).</strong>
        </p>
        <p style='color:rgba(255,255,255,0.3);font-size:14px;'>Hai domande? Scrivi a info@getautomatik.com</p>
    </div>
    </body></html>"""

@app.route("/onboarding", methods=["GET", "POST"])
def onboarding():
    if request.method == "GET":
        token = request.args.get("token", "")
        email = decode_onboarding_token(token)
        if not email:
            return render_template("onboarding.html", error="Link non valido o scaduto. Contatta info@getautomatik.com", success=False, token="", agency_name="", step=1)
        try:
            client = db.table("clients").select("company_name").eq("contact_email", email).execute()
            agency_name = client.data[0]["company_name"] if client.data else ""
        except Exception:
            agency_name = ""
        return render_template("onboarding.html", error=None, success=False, token=token, agency_name=agency_name, step=2)

    # POST — salva i dati
    token = request.form.get("token", "")
    email = decode_onboarding_token(token)
    if not email:
        return render_template("onboarding.html", error="Link non valido.", success=False, token="", agency_name="", step=1)

    agency_name = request.form.get("agency_name", "")
    portals = request.form.get("portals", "")
    lead_criteria = request.form.get("lead_criteria", "")
    contact_channel = request.form.get("contact_channel", "")
    work_hours = request.form.get("work_hours", "")

    try:
        db.table("clients").update({
            "company_name": agency_name,
            "onboarding_completed": True,
            "onboarding_data": json.dumps({
                "portals": portals,
                "lead_criteria": lead_criteria,
                "contact_channel": contact_channel,
                "work_hours": work_hours
            })
        }).eq("contact_email", email).execute()
    except Exception as e:
        print(f"Errore salvataggio onboarding: {e}")

    send_telegram(
        f"📋 ONBOARDING COMPLETATO!\n"
        f"Agenzia: {agency_name}\nEmail: {email}\n"
        f"Portali: {portals}\nCriteri: {lead_criteria}\n"
        f"Contatto: {contact_channel}\nOrari: {work_hours}"
    )
    return render_template("onboarding.html", error=None, success=True, token=token, agency_name=agency_name, step=3)



def _handle_stripe_event(event):
    try:
        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            email = (session.get("customer_details") or {}).get("email") or session.get("customer_email", "")
            name = (session.get("customer_details") or {}).get("name", email)
            is_trial = session.get("payment_status") == "no_payment_required"

            if email:
                try:
                    new_status = "trial" if is_trial else "active"
                    db.table("clients").update({"status": new_status}).eq("contact_email", email).eq("status", "payment_pending").execute()
                except Exception as e:
                    print(f"Errore aggiornamento cliente Stripe: {e}")

            if email:
                try:
                    db.table("prospects").update({"status": "converted"}).eq("contact_email", email).in_(
                        "status", ["contacted", "followup_1", "followup_2", "replied"]
                    ).execute()
                except Exception:
                    pass

            if is_trial:
                send_telegram(f"🎯 Nuovo trial attivato!\nCliente: {name}\nEmail: {email}\nTrial: 7 giorni gratuiti")
                if email:
                    send_onboarding_email(email, name or email.split("@")[0])
            else:
                amount = session.get("amount_total", 0)
                send_telegram(f"💳 Pagamento ricevuto!\nCliente: {name}\nEmail: {email}\nImporto: {amount / 100:.2f}€\nStato: ATTIVO")
                if email:
                    send_onboarding_email(email, name or email.split("@")[0])

        elif event["type"] == "invoice.payment_succeeded":
            invoice = event["data"]["object"]
            billing_reason = invoice.get("billing_reason", "")
            if billing_reason in ("subscription_cycle", "subscription_create"):
                customer_email = invoice.get("customer_email", "")
                if customer_email:
                    try:
                        db.table("clients").update({"status": "active"}).eq("contact_email", customer_email).eq("status", "trial").execute()
                    except Exception as e:
                        print(f"Errore attivazione post-trial: {e}")
                    send_telegram(f"💰 Cliente convertito da trial a pagante: {customer_email} - 197€/mese")

        elif event["type"] == "customer.subscription.deleted":
            import stripe as stripe_lib
            subscription = event["data"]["object"]
            customer_email = subscription.get("customer_email", "")
            if not customer_email:
                customer_id = subscription.get("customer", "")
                if customer_id:
                    try:
                        stripe_lib.api_key = os.getenv("STRIPE_SECRET_KEY")
                        customer = stripe_lib.Customer.retrieve(customer_id)
                        customer_email = customer.get("email", "")
                    except Exception:
                        pass
            if customer_email:
                try:
                    db.table("clients").update({"status": "cancelled"}).eq("contact_email", customer_email).execute()
                except Exception as e:
                    print(f"Errore cancellazione cliente: {e}")
                send_telegram(f"❌ Cancellazione abbonamento: {customer_email}")
    except Exception as e:
        print(f"Errore handler Stripe: {type(e).__name__}: {e}")

@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    import stripe as stripe_lib
    payload = request.get_data(as_text=False)
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        raw_event = json.loads(payload)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    is_live = raw_event.get("livemode", False)
    secret_key = "STRIPE_WEBHOOK_SECRET_LIVE" if is_live else "STRIPE_WEBHOOK_SECRET"
    webhook_secret = os.getenv(secret_key, "")
    if not webhook_secret:
        return jsonify({"error": "Secret not configured"}), 500

    try:
        stripe_lib.Webhook.construct_event(payload, sig_header, webhook_secret, tolerance=600)
    except Exception as e:
        print(f"Webhook verification failed: {type(e).__name__}: {e}")
        return jsonify({"error": "Invalid signature"}), 400

    # Return 200 immediately, process in background to avoid Stripe timeout
    threading.Thread(target=_handle_stripe_event, args=(raw_event,), daemon=True).start()
    return jsonify({"status": "ok"}), 200

@app.route("/webhook/calendly", methods=["POST"])
def calendly_webhook():
    """Triggered when someone books a call via Calendly."""
    try:
        data = request.json or {}
        event = data.get("event", "")
        payload = data.get("payload", {})

        if event == "invitee.created":
            invitee = payload.get("invitee", {})
            name = invitee.get("name", "")
            email = invitee.get("email", "")
            scheduled = payload.get("scheduled_event", {})
            start_time = scheduled.get("start_time", "")[:16].replace("T", " ") if scheduled.get("start_time") else "da definire"

            send_telegram(
                f"📅 CALL PRENOTATA!\n"
                f"Nome: {name}\nEmail: {email}\n"
                f"Orario: {start_time}"
            )

            # Find matching prospect and update status
            try:
                db.table("prospects").update({"status": "call_scheduled"}).eq("contact_email", email).execute()
            except Exception:
                pass

            # Send pre-call prep email
            if email:
                prep_body = (
                    f"Ciao {name or 'a te'},\n\n"
                    f"Perfetto! La tua call è confermata per {start_time}.\n\n"
                    f"Per sfruttare al massimo i 30 minuti, ti chiedo di avere pronti:\n"
                    f"• Il numero approssimativo di clienti che vuoi acquisire al mese\n"
                    f"• Il tuo settore principale e la città target\n"
                    f"• Come acquisisci clienti oggi (passaparola, Google Ads, ecc.)\n\n"
                    f"Saremo puntuali. A presto!\n\n"
                    f"Team GetAutomatik"
                )
                EMAIL = os.getenv("EMAIL_ADDRESS")
                EMAIL_PASS = os.getenv("EMAIL_PASSWORD")
                if EMAIL and EMAIL_PASS:
                    import smtplib
                    from email.mime.multipart import MIMEMultipart
                    from email.mime.text import MIMEText as MIMEText2
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = "La tua call GetAutomatik è confermata"
                    msg["From"] = f"GetAutomatik AI <{EMAIL}>"
                    msg["To"] = email
                    msg.attach(MIMEText2(prep_body, "plain", "utf-8"))
                    with smtplib.SMTP_SSL("smtp.zoho.eu", 465) as srv:
                        srv.login(EMAIL, EMAIL_PASS)
                        srv.send_message(msg)

        elif event == "invitee.canceled":
            invitee = payload.get("invitee", {})
            email = invitee.get("email", "")
            name = invitee.get("name", "")
            send_telegram(f"❌ Call cancellata: {name} ({email})")
            try:
                db.table("prospects").update({"status": "warm_2"}).eq("contact_email", email).eq("status", "call_scheduled").execute()
            except Exception:
                pass

    except Exception as e:
        print(f"Calendly webhook error: {e}")
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    try:
        db.table("metrics").insert({"date": datetime.now().date().isoformat()}).execute()
    except:
        pass
    threading.Thread(target=agency_loop, daemon=True).start()
    threading.Thread(target=imap_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)