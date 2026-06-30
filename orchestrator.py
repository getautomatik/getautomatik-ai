from flask import Flask, jsonify, request, redirect, render_template
from agents import (check_email_replies, send_followups, run_revenue_pipeline,
                    MetricsTracker, process_inbound_email, send_request_followups,
                    metrics_report, ceo_pivot, chat_qualify_lead, notify_chat_lead,
                    handle_missed_call, handle_sms_inbound, send_twilio_sms)
from supabase import create_client
from dotenv import load_dotenv
import os
import json
import threading
import time
import hmac as hmac_mod
import hashlib
import base64
import random
import string
from datetime import datetime, timedelta
import requests as req

load_dotenv()

app = Flask(__name__)
db = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
BUDGET = float(os.getenv("BUDGET_MENSILE", 500))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID")
FLOWOPS_DOMAIN = os.getenv("FLOWOPS_DOMAIN", "in.getautomatik.com")

DEFAULT_NICHES = ["fotovoltaico", "climatizzazione", "idraulici", "ristrutturazioni", "infissi"]

def generate_forwarding_address():
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"richieste-{suffix}@{FLOWOPS_DOMAIN}"


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

def imap_loop():
    time.sleep(120)
    while True:
        try:
            n = check_email_replies(db)
            if n:
                print(f"IMAP: {n} nuove risposte")
        except Exception as e:
            print(f"IMAP loop error: {e}")
        try:
            fu = send_request_followups(db)
            if fu:
                send_telegram(f"GetAutomatik: {fu} follow-up richieste inviati automaticamente")
        except Exception as e:
            print(f"Request followup loop error: {e}")
        time.sleep(1800)

def discovery_loop():
    """Runs every 24h: find new prospects via Apify across all sectors."""
    time.sleep(300)  # let other services start first
    while True:
        total_found = 0
        for sector in DEFAULT_NICHES:
            try:
                result = run_revenue_pipeline(db, sector=sector, hunt_count=8, audit_limit=0, send_limit=0)
                total_found += result.get("hunted", 0)
            except Exception as e:
                print(f"Discovery {sector} error: {e}")
        send_telegram(f"Discovery completata: {total_found} nuovi prospect su {len(DEFAULT_NICHES)} settori")
        time.sleep(86400)  # 24h


def outreach_loop():
    """Runs every 12h: audit qualified prospects, generate and send personalized emails."""
    time.sleep(600)
    iteration = 0
    while True:
        iteration += 1
        try:
            followups_sent = send_followups(db)
            if followups_sent:
                send_telegram(f"Sequenza follow-up: {followups_sent} email inviate")
        except Exception as e:
            print(f"Follow-up failed: {e}")
        summary_parts = []
        for sector in DEFAULT_NICHES:
            try:
                result = run_revenue_pipeline(db, sector=sector, hunt_count=0, audit_limit=4, send_limit=6)
                if result.get("sent", 0):
                    summary_parts.append(f"{sector}: {result['sent']} email")
            except Exception as e:
                print(f"Outreach {sector} error: {e}")
        if summary_parts:
            send_telegram(f"Outreach ciclo {iteration}: " + ", ".join(summary_parts))
        time.sleep(43200)  # 12h


def pivot_loop():
    """Runs every 6h: CEO analyzes reply rates and pivots sector priorities."""
    time.sleep(1800)
    iteration = 0
    while True:
        iteration += 1
        try:
            ceo_pivot(db)
        except Exception as e:
            print(f"CEO pivot error: {e}")
        if iteration % 12 == 0:  # every 72h (12 * 6h)
            try:
                metrics_report(db)
            except Exception as e:
                print(f"Metrics report error: {e}")
        time.sleep(21600)  # 6h

@app.route("/")
def landing():
    ref = request.args.get("ref", "")
    html = open("templates/landing.html", "r", encoding="utf-8").read()
    if ref:
        html = html.replace('href="/onboarding"', f'href="/onboarding?ref={ref}"')
        html = html.replace('href="/checkout"', f'href="/checkout?ref={ref}"')
    return html

@app.route("/landing")
def landing_redirect():
    ref = request.args.get("ref", "")
    url = f"/?ref={ref}" if ref else "/"
    return redirect(url)

@app.route("/dashboard")
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
                    <div style="margin-left:auto;font-size:13px;color:rgba(255,255,255,0.3);">warm_1 → warm_2 → warm_closed → converted</div>
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
        
        <div class="pipeline-section" style="margin-bottom:20px;">
            <div class="section-title">Sales Loop — Metriche</div>
            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:16px;">
                <div style="text-align:center;padding:12px;background:rgba(255,255,255,0.03);border-radius:10px;">
                    <div style="font-size:22px;font-weight:900;color:#fff;" id="m-p-today">0</div>
                    <div style="font-size:11px;color:rgba(255,255,255,0.4);margin-top:3px;">Prospect oggi</div>
                </div>
                <div style="text-align:center;padding:12px;background:rgba(255,255,255,0.03);border-radius:10px;">
                    <div style="font-size:22px;font-weight:900;color:#fff;" id="m-p-week">0</div>
                    <div style="font-size:11px;color:rgba(255,255,255,0.4);margin-top:3px;">Questa settimana</div>
                </div>
                <div style="text-align:center;padding:12px;background:rgba(255,255,255,0.03);border-radius:10px;">
                    <div style="font-size:22px;font-weight:900;color:#fff;" id="m-p-total">0</div>
                    <div style="font-size:11px;color:rgba(255,255,255,0.4);margin-top:3px;">Totale prospect</div>
                </div>
                <div style="text-align:center;padding:12px;background:rgba(255,255,255,0.03);border-radius:10px;">
                    <div style="font-size:22px;font-weight:900;color:#00e87a;" id="m-best">n/d</div>
                    <div style="font-size:11px;color:rgba(255,255,255,0.4);margin-top:3px;">Settore top</div>
                </div>
            </div>
            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:16px;">
                <div style="text-align:center;padding:12px;background:rgba(255,255,255,0.03);border-radius:10px;">
                    <div style="font-size:22px;font-weight:900;color:#00b4d8;" id="m-emails">0</div>
                    <div style="font-size:11px;color:rgba(255,255,255,0.4);margin-top:3px;">Email inviate</div>
                </div>
                <div style="text-align:center;padding:12px;background:rgba(255,255,255,0.03);border-radius:10px;">
                    <div style="font-size:22px;font-weight:900;color:#ffaa00;" id="m-rr">0%</div>
                    <div style="font-size:11px;color:rgba(255,255,255,0.4);margin-top:3px;">Reply rate</div>
                </div>
                <div style="text-align:center;padding:12px;background:rgba(255,255,255,0.03);border-radius:10px;">
                    <div style="font-size:22px;font-weight:900;color:#00e87a;" id="m-clients">0</div>
                    <div style="font-size:11px;color:rgba(255,255,255,0.4);margin-top:3px;">Clienti attivi</div>
                </div>
                <div style="text-align:center;padding:12px;background:rgba(255,255,255,0.03);border-radius:10px;">
                    <div style="font-size:22px;font-weight:900;color:#00e87a;" id="m-mrr">€0</div>
                    <div style="font-size:11px;color:rgba(255,255,255,0.4);margin-top:3px;">MRR</div>
                </div>
            </div>
            <table style="width:100%;border-collapse:collapse;">
                <thead><tr style="font-size:11px;color:rgba(255,255,255,0.3);text-transform:uppercase;letter-spacing:1px;">
                    <th style="text-align:left;padding:6px 12px;">Settore</th>
                    <th style="text-align:left;padding:6px 12px;">Contattati</th>
                    <th style="text-align:left;padding:6px 12px;">Risposte</th>
                    <th style="text-align:left;padding:6px 12px;">Reply Rate</th>
                </tr></thead>
                <tbody id="m-sector-table"></tbody>
            </table>
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

        <div class="pipeline-section" style="margin-bottom:30px;border-color:rgba(0,232,122,0.25);background:linear-gradient(135deg,rgba(0,232,122,0.04),rgba(0,180,216,0.04));">
            <div class="section-title" style="color:#00e87a;">GetAutomatik ROI questo mese</div>
            <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:16px;margin-bottom:20px;" id="roi-grid">
                <div style="text-align:center;">
                    <div style="font-size:28px;font-weight:900;color:#fff;" id="roi-requests">0</div>
                    <div style="font-size:11px;color:rgba(255,255,255,0.4);text-transform:uppercase;letter-spacing:1px;margin-top:4px;">Richieste</div>
                </div>
                <div style="text-align:center;">
                    <div style="font-size:28px;font-weight:900;color:#00b4d8;" id="roi-avg-time">0 min</div>
                    <div style="font-size:11px;color:rgba(255,255,255,0.4);text-transform:uppercase;letter-spacing:1px;margin-top:4px;">Tempo medio risposta</div>
                </div>
                <div style="text-align:center;">
                    <div style="font-size:28px;font-weight:900;color:#ffaa00;" id="roi-total-value">€0</div>
                    <div style="font-size:11px;color:rgba(255,255,255,0.4);text-transform:uppercase;letter-spacing:1px;margin-top:4px;">Valore richieste</div>
                </div>
                <div style="text-align:center;">
                    <div style="font-size:28px;font-weight:900;color:#00e87a;" id="roi-converted">0</div>
                    <div style="font-size:11px;color:rgba(255,255,255,0.4);text-transform:uppercase;letter-spacing:1px;margin-top:4px;">Convertiti</div>
                </div>
                <div style="text-align:center;">
                    <div style="font-size:28px;font-weight:900;color:#00e87a;" id="roi-recovered">€0</div>
                    <div style="font-size:11px;color:rgba(255,255,255,0.4);text-transform:uppercase;letter-spacing:1px;margin-top:4px;">Valore recuperato</div>
                </div>
            </div>
            <div style="background:rgba(0,232,122,0.08);border:1px solid rgba(0,232,122,0.2);border-radius:10px;padding:14px 20px;text-align:center;">
                <span style="font-size:15px;font-weight:700;color:#00e87a;" id="roi-summary">GetAutomatik ha risparmiato 0 ore e recuperato €0 questo mese</span>
            </div>
        </div>

        <div class="log-section">
            <div class="section-title">Activity Log</div>
            <div id="log-container">
                <div class="log-entry"><span class="log-time">--:--</span><span class="log-agent">Sistema</span>In attesa del primo ciclo...</div>
            </div>
        </div>

        <div class="footer">GetAutomatik AI © 2026 | Artigiani italiani | Budget: <span id="budget-footer">500€</span></div>
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

        async function updateROI() {
            try {
                const resp = await fetch('/api/roi');
                const d = await resp.json();
                document.getElementById('roi-requests').textContent = d.total_requests;
                document.getElementById('roi-avg-time').textContent = d.avg_response_min + ' min';
                document.getElementById('roi-total-value').textContent = '€' + (d.total_value || 0).toLocaleString('it-IT');
                document.getElementById('roi-converted').textContent = d.converted_count;
                document.getElementById('roi-recovered').textContent = '€' + (d.recovered_value || 0).toLocaleString('it-IT');
                document.getElementById('roi-summary').textContent =
                    'GetAutomatik ti ha fatto risparmiare ' + d.hours_saved + ' ore e recuperato €' +
                    (d.recovered_value || 0).toLocaleString('it-IT') + ' questo mese';
            } catch(e) {}
        }

        async function updateMetrics() {
            try {
                const resp = await fetch('/api/metrics');
                const d = await resp.json();
                const el = id => document.getElementById(id);
                if (el('m-p-today')) el('m-p-today').textContent = d.prospects_today;
                if (el('m-p-week')) el('m-p-week').textContent = d.prospects_week;
                if (el('m-p-total')) el('m-p-total').textContent = d.prospects_total;
                if (el('m-emails')) el('m-emails').textContent = d.emails_sent;
                if (el('m-rr')) el('m-rr').textContent = d.overall_reply_rate + '%';
                if (el('m-mrr')) el('m-mrr').textContent = '€' + (d.mrr || 0);
                if (el('m-clients')) el('m-clients').textContent = d.active_clients;
                if (el('m-req-val')) el('m-req-val').textContent = '€' + (d.requests_value_month || 0).toLocaleString('it-IT');
                if (el('m-best')) el('m-best').textContent = d.best_sector;
                if (el('m-sector-table') && d.sector_stats && d.sector_stats.length) {
                    el('m-sector-table').innerHTML = d.sector_stats.map(s =>
                        `<tr><td style="padding:8px 12px;font-weight:600;text-transform:capitalize;">${s.sector}</td>
                         <td style="padding:8px 12px;">${s.contacted}</td>
                         <td style="padding:8px 12px;">${s.replied}</td>
                         <td style="padding:8px 12px;color:${s.reply_rate>5?'#00e87a':s.reply_rate>2?'#ffaa00':'#ff6b35'};font-weight:700;">${s.reply_rate}%</td></tr>`
                    ).join('');
                }
            } catch(e) {}
        }

        setInterval(updateDashboard, 5000);
        setInterval(updateLogs, 10000);
        setInterval(updatePipeline, 30000);
        setInterval(updatePL, 60000);
        setInterval(updateROI, 60000);
        setInterval(updateMetrics, 30000);
        updateDashboard();
        updateLogs();
        updatePipeline();
        updatePL();
        updateROI();
        updateMetrics();
    </script>
</body>
</html>"""


@app.route("/api/status")
def status():
    clients = db.table("clients").select("*").eq("status", "active").execute()
    prospects = db.table("prospects").select("*").execute()
    markets = db.table("markets").select("*").eq("active", True).execute()
    warm = db.table("prospects").select("id").in_("status", ["warm_1", "warm_2"]).execute()
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
                                      "warm_closed", "converted", "dead"):
                pipeline[s]["contacted"] += 1
            if p.get("status") in ("replied", "warm_1", "warm_2", "warm_closed", "converted"):
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


@app.route("/api/metrics")
def metrics_endpoint():
    try:
        today = datetime.now().date().isoformat()
        week_ago = (datetime.now().date() - timedelta(days=7)).isoformat()
        month_start = datetime.now().replace(day=1).date().isoformat()

        # Prospects counts
        all_p = db.table("prospects").select("sector,status,created_at").execute()
        rows = all_p.data or []
        today_p = sum(1 for r in rows if (r.get("created_at") or "")[:10] == today)
        week_p = sum(1 for r in rows if (r.get("created_at") or "")[:10] >= week_ago)
        total_p = len(rows)

        # Per-sector reply rate
        sectors = {}
        for r in rows:
            s = r.get("sector", "altro")
            if s not in sectors:
                sectors[s] = {"contacted": 0, "replied": 0}
            if r.get("status") in ("contacted", "followup_1", "replied", "warm_1", "warm_2", "warm_closed", "converted", "dead"):
                sectors[s]["contacted"] += 1
            if r.get("status") in ("replied", "warm_1", "warm_2", "warm_closed", "converted"):
                sectors[s]["replied"] += 1
        sector_stats = [
            {
                "sector": s,
                "contacted": d["contacted"],
                "replied": d["replied"],
                "reply_rate": round(d["replied"] / max(d["contacted"], 1) * 100, 1)
            }
            for s, d in sorted(sectors.items(), key=lambda x: x[1]["replied"], reverse=True)
        ]
        best_sector = sector_stats[0]["sector"] if sector_stats else "n/d"

        # Active clients and MRR
        clients = db.table("clients").select("mrr,status").execute()
        active_clients = [c for c in (clients.data or []) if c.get("status") in ("active", "trial")]
        mrr = sum(c.get("mrr") or 0 for c in active_clients)

        # Emails sent
        total_emailed = sum(d["contacted"] for d in sectors.values())
        total_replied = sum(d["replied"] for d in sectors.values())
        overall_rr = round(total_replied / max(total_emailed, 1) * 100, 1)

        # Requests value this month
        reqs = db.table("requests").select("estimated_value,converted").gte("received_at", month_start + "T00:00:00").execute()
        req_data = reqs.data or []
        req_value = sum(r.get("estimated_value") or 0 for r in req_data)

        return jsonify({
            "prospects_today": today_p,
            "prospects_week": week_p,
            "prospects_total": total_p,
            "emails_sent": total_emailed,
            "replies": total_replied,
            "overall_reply_rate": overall_rr,
            "active_clients": len(active_clients),
            "mrr": mrr,
            "requests_value_month": req_value,
            "best_sector": best_sector,
            "sector_stats": sector_stats,
        })
    except Exception as e:
        print(f"metrics_endpoint error: {e}")
        return jsonify({
            "prospects_today": 0, "prospects_week": 0, "prospects_total": 0,
            "emails_sent": 0, "replies": 0, "overall_reply_rate": 0,
            "active_clients": 0, "mrr": 0, "requests_value_month": 0,
            "best_sector": "n/d", "sector_stats": [],
        })


@app.route("/api/roi")
def roi_report():
    try:
        from_date = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        rows = db.table("requests").select(
            "id,estimated_value,converted,followup_count,received_at,replied_at,lead_type"
        ).gte("received_at", from_date).execute()
        data = rows.data or []

        total_requests = len(data)
        total_value = sum(r.get("estimated_value") or 0 for r in data)
        converted = [r for r in data if r.get("converted")]
        converted_count = len(converted)
        recovered_value = sum(r.get("estimated_value") or 0 for r in converted)

        # Average response time in minutes
        response_times = []
        for r in data:
            try:
                recv = datetime.fromisoformat(r["received_at"].replace("Z", "+00:00"))
                repl = datetime.fromisoformat(r["replied_at"].replace("Z", "+00:00"))
                diff_min = abs((repl - recv).total_seconds()) / 60
                if diff_min < 1440:  # only count if < 24h (sanity check)
                    response_times.append(diff_min)
            except Exception:
                pass
        avg_response_min = round(sum(response_times) / len(response_times), 1) if response_times else 0

        # Hours saved: assume 15 min per manual response
        hours_saved = round(total_requests * 15 / 60, 1)

        return jsonify({
            "total_requests": total_requests,
            "total_value": total_value,
            "converted_count": converted_count,
            "recovered_value": recovered_value,
            "avg_response_min": avg_response_min,
            "hours_saved": hours_saved,
        })
    except Exception as e:
        print(f"roi_report error: {e}")
        return jsonify({
            "total_requests": 0, "total_value": 0, "converted_count": 0,
            "recovered_value": 0, "avg_response_min": 0, "hours_saved": 0,
        })


@app.route("/webhook/signup", methods=["POST"])
def signup():
    data = request.json or request.form.to_dict()
    company = data.get("company", "")
    email = data.get("email", "")
    sector = data.get("sector", "artigiani")
    prospect_ref = data.get("ref", "")

    if company and email:
        try:
            db.table("clients").insert({
                "company_name": company,
                "contact_email": email,
                "sector": sector,
                "plan": "monthly",
                "mrr": 0,
                "status": "trial",
            }).execute()
            # Link prospect to client if ref provided
            if prospect_ref:
                try:
                    db.table("prospects").update({
                        "status": "converted",
                        "converted_at": datetime.now().isoformat(),
                    }).eq("id", prospect_ref).execute()
                except Exception as e:
                    print(f"prospect link error: {e}")
            send_telegram(
                f"NUOVO CLIENTE: {company} — 197 EUR/mese trial avviato\n"
                f"Settore: {sector} | Email: {email}"
                + (f"\nProspect collegato: {prospect_ref}" if prospect_ref else "")
            )
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

@app.route("/onboarding")
def onboarding():
    return render_template("onboarding.html")


@app.route("/api/onboarding/generate", methods=["POST"])
def onboarding_generate():
    import anthropic
    data = request.json or {}
    nome_azienda = data.get("nome_azienda", "").strip()
    settore = data.get("settore", "").strip()
    email_titolare = data.get("email_titolare", "").strip()

    if not nome_azienda or not settore or not email_titolare:
        return jsonify({"error": "Dati mancanti"}), 400

    forwarding_address = generate_forwarding_address()

    SETTORI = {
        "fotovoltaico": "pannelli solari e impianti fotovoltaici",
        "climatizzazione": "climatizzazione e condizionatori",
        "idraulici": "impianti idraulici e riparazioni",
        "ristrutturazioni": "ristrutturazioni edili",
        "infissi": "infissi, finestre e porte",
    }
    settore_desc = SETTORI.get(settore, settore)

    try:
        client_ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        prompt = (
            f"Immagina di essere l'assistente AI di '{nome_azienda}', "
            f"un'azienda di {settore_desc}.\n\n"
            f"Scrivi un messaggio di benvenuto breve (2-3 frasi) che mostra come risponderesti "
            f"a un cliente che chiede un preventivo. Sii professionale, cordiale e specifico per il settore {settore}. "
            f"Usa il nome dell'azienda nel messaggio."
        )
        msg = client_ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        aha_response = msg.content[0].text.strip()
    except Exception as e:
        print(f"Claude AHA error: {e}")
        aha_response = (
            f"Grazie per aver contattato {nome_azienda}! "
            f"Abbiamo ricevuto la sua richiesta per {settore_desc} "
            f"e la contatteremo entro poche ore con un preventivo personalizzato."
        )

    return jsonify({
        "forwarding_address": forwarding_address,
        "aha_response": aha_response,
    })


@app.route("/api/onboarding/save", methods=["POST"])
def onboarding_save():
    data = request.json or {}
    nome_azienda = data.get("nome_azienda", "").strip()
    settore = data.get("settore", "").strip()
    email_titolare = data.get("email_titolare", "").strip()
    forwarding_address = data.get("forwarding_address", "").strip()

    if not all([nome_azienda, settore, email_titolare, forwarding_address]):
        return jsonify({"error": "Dati mancanti"}), 400

    try:
        chat_token = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
        existing = db.table("client_configs").select("id", "chat_token").eq("email_titolare", email_titolare).execute()
        if existing.data:
            existing_token = existing.data[0].get("chat_token") or chat_token
            db.table("client_configs").update({
                "forwarding_address": forwarding_address,
                "settore": settore,
                "nome_azienda": nome_azienda,
                "status": "active",
                "chat_token": existing_token,
            }).eq("email_titolare", email_titolare).execute()
            chat_token = existing_token
        else:
            db.table("client_configs").insert({
                "forwarding_address": forwarding_address,
                "settore": settore,
                "nome_azienda": nome_azienda,
                "email_titolare": email_titolare,
                "status": "active",
                "chat_token": chat_token,
            }).execute()
    except Exception as e:
        print(f"Errore salvataggio client_configs: {e}")
        return jsonify({"error": "Errore database"}), 500

    send_telegram(
        f"GetAutomatik nuovo cliente!\n"
        f"Azienda: {nome_azienda} ({settore})\n"
        f"Email: {email_titolare}\n"
        f"Forwarding: {forwarding_address}"
    )
    widget_snippet = f'<script src="https://getautomatik.com/widget.js?cid={chat_token}" defer></script>'
    return jsonify({"status": "ok", "chat_token": chat_token, "widget_snippet": widget_snippet})


@app.route("/webhook/email-inbound", methods=["POST"])
def email_inbound():
    content_type = request.content_type or ""
    if "application/json" in content_type:
        data = request.json or {}
        to_address = data.get("To", data.get("OriginalRecipient", "")).lower().strip()
        from_email = data.get("From", "").strip()
        from_name = data.get("FromName", "")
        subject = data.get("Subject", "")
        body_text = data.get("TextBody", data.get("body-plain", ""))
    else:
        to_address = (request.form.get("recipient") or request.form.get("To", "")).lower().strip()
        from_email = (request.form.get("sender") or request.form.get("From", "")).strip()
        from_name = request.form.get("from_name", "")
        subject = request.form.get("subject", "")
        body_text = request.form.get("body-plain", request.form.get("stripped-text", ""))

    if not to_address or not from_email:
        return jsonify({"error": "Dati email incompleti"}), 400

    import re as re_mod
    m = re_mod.search(r'<([^>]+)>', from_email)
    if m:
        from_name = from_name or from_email.split("<")[0].strip().strip('"')
        from_email = m.group(1)
    m2 = re_mod.search(r'<([^>]+)>', to_address)
    if m2:
        to_address = m2.group(1)

    threading.Thread(
        target=process_inbound_email,
        args=(db, to_address, from_email, from_name, subject, body_text),
        daemon=True
    ).start()
    return jsonify({"status": "ok"}), 200


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

@app.route("/widget.js")
def widget_js():
    cid = request.args.get("cid", "")
    js = f"""(function(){{
  var CID="{cid}";
  var API="https://getautomatik.com/api/chat";
  var history=[];
  var style=document.createElement("style");
  style.textContent=`
    #ga-bubble{{position:fixed;bottom:24px;right:24px;width:56px;height:56px;border-radius:50%;background:#00e87a;cursor:pointer;z-index:99999;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 16px rgba(0,232,122,.4);transition:transform .2s}}
    #ga-bubble:hover{{transform:scale(1.08)}}
    #ga-bubble svg{{width:28px;height:28px;fill:#0a0a0a}}
    #ga-win{{position:fixed;bottom:96px;right:24px;width:340px;max-height:480px;background:#111;border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,.5);z-index:99998;display:none;flex-direction:column;overflow:hidden;border:1px solid #222}}
    #ga-head{{background:#00e87a;padding:14px 18px;font-family:sans-serif;font-size:14px;font-weight:700;color:#0a0a0a}}
    #ga-msgs{{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px;font-family:sans-serif;font-size:13px;min-height:200px;max-height:320px}}
    .ga-msg-ai{{align-self:flex-start;background:#1e1e1e;color:#e0e0e0;padding:10px 14px;border-radius:0 12px 12px 12px;max-width:80%}}
    .ga-msg-user{{align-self:flex-end;background:#00e87a;color:#0a0a0a;padding:10px 14px;border-radius:12px 0 12px 12px;max-width:80%}}
    #ga-input-row{{display:flex;padding:10px;gap:8px;border-top:1px solid #222}}
    #ga-input{{flex:1;background:#1e1e1e;border:1px solid #333;color:#fff;border-radius:8px;padding:8px 12px;font-size:13px;outline:none}}
    #ga-send{{background:#00e87a;color:#0a0a0a;border:none;border-radius:8px;padding:8px 14px;font-weight:700;cursor:pointer;font-size:13px}}
  `;
  document.head.appendChild(style);
  var bubble=document.createElement("div");bubble.id="ga-bubble";
  bubble.innerHTML='<svg viewBox="0 0 24 24"><path d="M20 2H4a2 2 0 00-2 2v18l4-4h14a2 2 0 002-2V4a2 2 0 00-2-2z"/></svg>';
  document.body.appendChild(bubble);
  var win=document.createElement("div");win.id="ga-win";
  win.innerHTML='<div id="ga-head">Ciao! Come possiamo aiutarti?</div><div id="ga-msgs"></div><div id="ga-input-row"><input id="ga-input" placeholder="Scrivi un messaggio..."/><button id="ga-send">Invia</button></div>';
  document.body.appendChild(win);
  var msgs=document.getElementById("ga-msgs");
  var input=document.getElementById("ga-input");
  var open=false;
  function addMsg(text,role){{var d=document.createElement("div");d.className=role==="user"?"ga-msg-user":"ga-msg-ai";d.textContent=text;msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;}}
  function send(){{
    var msg=input.value.trim();if(!msg)return;
    addMsg(msg,"user");input.value="";
    history.push({{"role":"user","content":msg}});
    fetch(API,{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{cid:CID,history:history}})}})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      addMsg(d.reply,"ai");
      history.push({{"role":"assistant","content":d.reply}});
      if(d.qualified){{setTimeout(function(){{win.style.display="none";open=false;}},3000);}}
    }}).catch(function(){{addMsg("Errore di connessione. Riprova.","ai");}});
  }}
  bubble.onclick=function(){{open=!open;win.style.display=open?"flex":"none";if(open&&history.length===0){{fetch(API,{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{cid:CID,history:[]}})}}).then(function(r){{return r.json();}}).then(function(d){{addMsg(d.reply,"ai");history.push({{"role":"assistant","content":d.reply}});}});}}}};
  document.getElementById("ga-send").onclick=send;
  input.onkeydown=function(e){{if(e.key==="Enter")send();}};
}})();"""
    resp = app.response_class(response=js, status=200, mimetype="application/javascript")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/api/chat", methods=["POST", "OPTIONS"])
def api_chat():
    if request.method == "OPTIONS":
        r = app.response_class(status=200)
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type"
        r.headers["Access-Control-Allow-Methods"] = "POST"
        return r
    data = request.json or {}
    cid = data.get("cid", "").strip()
    history = data.get("history", [])

    # Lookup client by chat_token
    client_config = None
    if cid:
        try:
            r = db.table("client_configs").select("*").eq("chat_token", cid).eq("active", True).execute()
            if r.data:
                client_config = r.data[0]
        except Exception as e:
            print(f"api_chat lookup error: {e}")

    if not client_config:
        client_config = {"settore": "servizi", "nome_azienda": "l'azienda", "email_titolare": None}

    result = chat_qualify_lead(client_config, history)
    reply = result.get("reply", "Ciao! Come posso aiutarti?")

    if result.get("qualified"):
        try:
            db.table("chat_sessions").insert({
                "client_config_id": client_config.get("id"),
                "lead_name": result.get("lead_name"),
                "lead_phone": result.get("lead_phone"),
                "lead_type": result.get("lead_type"),
                "messages": history,
                "qualified": True,
            }).execute()
        except Exception as e:
            print(f"chat_sessions save error: {e}")
        notify_chat_lead(client_config, result.get("lead_name"), result.get("lead_phone"), result.get("lead_type"))

    resp = jsonify({"reply": reply, "qualified": result.get("qualified", False)})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/webhook/missed-call", methods=["POST"])
def missed_call_webhook():
    """Twilio webhook: incoming call that was not answered."""
    twilio_number = request.form.get("To", "").strip()
    caller_phone = request.form.get("From", "").strip()
    call_status = request.form.get("CallStatus", "").lower()
    if caller_phone and twilio_number and call_status in ("no-answer", "busy", "canceled", ""):
        threading.Thread(
            target=handle_missed_call,
            args=(db, twilio_number, caller_phone),
            daemon=True,
        ).start()
    # Return empty TwiML
    return app.response_class(
        response='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        status=200,
        mimetype="text/xml",
    )


@app.route("/webhook/sms-inbound", methods=["POST"])
def sms_inbound_webhook():
    """Twilio webhook: incoming SMS reply."""
    twilio_number = request.form.get("To", "").strip()
    from_phone = request.form.get("From", "").strip()
    body = request.form.get("Body", "").strip()
    if twilio_number and from_phone and body:
        threading.Thread(
            target=handle_sms_inbound,
            args=(db, twilio_number, from_phone, body),
            daemon=True,
        ).start()
    return app.response_class(
        response='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        status=200,
        mimetype="text/xml",
    )


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


def _start_background_threads():
    try:
        db.table("metrics").insert({"date": datetime.now().date().isoformat()}).execute()
    except:
        pass
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=outreach_loop, daemon=True).start()
    threading.Thread(target=pivot_loop, daemon=True).start()
    # imap_loop disabilitato: IMAP non disponibile su piano Zoho attuale
    send_telegram("GetAutomatik AI avviata: discovery 24h, outreach 12h, pivot 6h")

threading.Thread(target=_start_background_threads, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))




