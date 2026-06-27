import os, json, requests as req
from supabase import create_client

def scrape_google_maps(db, params):
    sector = params.get("sector", "immobiliare")
    location = params.get("location", "Milano")
    count = params.get("count", 5)
    prospects = []
    for i in range(1, count + 1):
        prospects.append({"company_name": f"{sector.title()} {location} {i}", "contact_email": f"info{location.lower()}{i}@esempio.it", "sector": sector, "source": "google_maps", "score": 7})
    for p in prospects:
        db.table("prospects").insert(p).execute()
    existing = db.table("markets").select("*").eq("sector", sector).execute()
    if existing.data:
        db.table("markets").update({"leads_found": existing.data[0]["leads_found"] + len(prospects)}).eq("sector", sector).execute()
    else:
        db.table("markets").insert({"sector": sector, "score": 50, "leads_found": len(prospects)}).execute()
    return {"prospects_found": len(prospects), "sector": sector, "location": location}

def evaluate_market(db, params):
    sector = params.get("sector")
    prospects = db.table("prospects").select("*").eq("sector", sector).execute()
    clients = db.table("clients").select("*").eq("sector", sector).execute()
    total = len(prospects.data) if prospects.data else 0
    converted = len(clients.data) if clients.data else 0
    conversion = (converted/total*100) if total > 0 else 0
    score = min(100, int(conversion * 10 + (total * 0.5)))
    db.table("markets").update({"score": score, "conversion_rate": round(conversion, 1)}).eq("sector", sector).execute()
    return {"sector": sector, "score": score, "conversion_rate": conversion}

def generate_email(db, params):
    import anthropic
    import smtplib
    from email.mime.text import MIMEText
    claude = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
    prospect = params.get("prospect", {})
    company = prospect.get("company_name", "cliente")
    sector = prospect.get("sector", "azienda")
    email_to = prospect.get("contact_email", "")
    prompt = f"Scrivi email vendita per: {company}, settore: {sector}. Servizio: Agente AI per acquisizione clienti. Includi CTA per demo gratuita. Max 120 parole."
    response = claude.messages.create(model="claude-haiku-4-5-20251001", max_tokens=200, messages=[{"role": "user", "content": prompt}])
    email_body = response.content[0].text
    email_from = os.getenv("EMAIL_ADDRESS")
    email_pass = os.getenv("EMAIL_PASSWORD")
    sent = False
    if email_from and email_pass and email_to:
        try:
            msg = MIMEText(email_body)
            msg["Subject"] = f"Automatizza la crescita di {company}"
            msg["From"] = email_from
            msg["To"] = email_to
            with smtplib.SMTP_SSL("smtp.zoho.eu", 465) as server:
                server.login(email_from, email_pass)
                server.send_message(msg)
            sent = True
        except Exception as e:
            print(f"Errore invio email: {e}")
    db.table("prospects").update({"status": "contacted", "agent_notes": email_body[:100]}).eq("id", prospect.get("id")).execute()
    return {"email": email_body[:100], "sent": sent, "prospect": company}

def send_linkedin_connection(db, params):
    return {"status": "simulato"}

def qualify_lead(db, params):
    import anthropic
    claude = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
    client_id = params.get("client_id")
    lead_data = params.get("lead", {})
    client = db.table("clients").select("*").eq("id", client_id).execute()
    sector = client.data[0]["sector"] if client.data else "generico"
    prompt = f"Qualifica lead per {sector}. Lead: {lead_data}. Rispondi JSON: qualified (true/false), score (1-10), reason."
    response = claude.messages.create(model="claude-haiku-4-5-20251001", max_tokens=100, messages=[{"role": "user", "content": prompt}])
    try:
        result = json.loads(response.content[0].text)
    except:
        result = {"qualified": False, "score": 3, "reason": "errore"}
    db.table("leads").insert({"client_id": client_id, "name": lead_data.get("name","Sconosciuto"), "contact": lead_data.get("contact",""), "qualified": result.get("qualified",False), "score": result.get("score",5), "notes": json.dumps(result)}).execute()
    return result

def generate_report(db, params):
    client_id = params.get("client_id")
    leads = db.table("leads").select("*").eq("client_id", client_id).execute()
    total = len(leads.data) if leads.data else 0
    qualified = len([l for l in leads.data if l["qualified"]]) if leads.data else 0
    return {"total_leads": total, "qualified": qualified, "conversion": f"{(qualified/total*100):.1f}%" if total > 0 else "0%"}

def analyze_markets(db, params):
    markets = db.table("markets").select("*").execute()
    analysis = []
    for m in (markets.data or []):
        recommendation = "INVESTI" if m["score"] > 60 else "MONITORA" if m["score"] > 30 else "FERMA"
        analysis.append({"sector": m["sector"], "score": m["score"], "recommendation": recommendation})
    return {"markets": analysis}

def suggest_pricing(db, params):
    clients = db.table("clients").select("*").eq("status", "active").execute()
    total = len(clients.data) if clients.data else 0
    return {"current_price": 197, "suggested": 297 if total > 3 else 197}

def predict_churn(db, params):
    clients = db.table("clients").select("*").eq("status", "active").execute()
    at_risk = []
    for c in (clients.data or []):
        leads = db.table("leads").select("*").eq("client_id", c["id"]).execute()
        total = len(leads.data) if leads.data else 0
        if total < 5:
            at_risk.append({"client": c["company_name"], "risk": "ALTO"})
    return {"at_risk": at_risk}