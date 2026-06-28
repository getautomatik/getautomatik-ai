import os, json, requests as req, time, re, random
from datetime import date, timedelta
from dotenv import load_dotenv
load_dotenv()
from supabase import create_client

def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        req.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": message})
    except:
        pass

def _is_real_email(email):
    if not email:
        return False
    fake_domains = ["gmail", "yahoo", "hotmail", "outlook", "libero", "virgilio", "example"]
    domain = email.split("@")[-1].lower() if "@" in email else ""
    return bool(domain) and not any(d in domain for d in fake_domains)

def _scrape_email_from_website(website, apify_token):
    import requests as r
    import time
    if not website:
        return None
    try:
        run_url = "https://api.apify.com/v2/acts/vdrmota~contact-info-scraper/runs"
        resp = r.post(run_url,
            params={"token": apify_token},
            json={"startUrls": [{"url": website}], "maxDepth": 1, "maxPages": 3},
            timeout=15)
        if resp.status_code != 201:
            print(f"vdrmota scraper start failed: {resp.status_code} {resp.text[:100]}")
            return None
        run_id = resp.json()["data"]["id"]
        time.sleep(60)
        results_url = f"https://api.apify.com/v2/acts/vdrmota~contact-info-scraper/runs/{run_id}/dataset/items"
        results_resp = r.get(results_url, params={"token": apify_token}, timeout=15)
        if results_resp.status_code != 200:
            return None
        for item in results_resp.json():
            for email in item.get("emails", []):
                if _is_real_email(email):
                    return email
    except Exception as e:
        print(f"Contact scraper error: {e}")
    return None

def scrape_google_maps(db, params):
    import requests as r
    import os, time
    sector = params.get("sector", "immobiliare")
    location = params.get("location", "Milano")
    count = min(params.get("count", 10), 20)
    apify_token = os.getenv("APIFY_API_KEY", "")

    raw_results = []

    if apify_token and apify_token != "il-tuo-token":
        try:
            run_url = "https://api.apify.com/v2/acts/compass~crawler-google-places/runs"
            resp = r.post(run_url,
                params={"token": apify_token},
                json={
                    "searchStrings": [f"{sector} {location}"],
                    "maxCrawledPlaces": count,
                    "language": "it"
                })
            if resp.status_code == 201:
                run_id = resp.json()["data"]["id"]
                time.sleep(90)
                results_url = f"https://api.apify.com/v2/acts/compass~crawler-google-places/runs/{run_id}/dataset/items"
                results_resp = r.get(results_url, params={"token": apify_token})
                if results_resp.status_code == 200:
                    for item in results_resp.json()[:count]:
                        name = item.get("title", "")
                        if not name:
                            continue
                        raw_results.append({
                            "company_name": name,
                            "contact_email": item.get("email", ""),
                            "contact_phone": item.get("phone", ""),
                            "website": item.get("website", ""),
                            "sector": sector,
                            "source": "apify_maps",
                            "score": 8
                        })
        except Exception as e:
            print(f"Apify error: {e}")

    # Per ogni risultato senza email reale, prova a scrapare il sito
    prospects = []
    for p in raw_results:
        if _is_real_email(p["contact_email"]):
            prospects.append(p)
        elif p["website"] and apify_token:
            email = _scrape_email_from_website(p["website"], apify_token)
            if email:
                p["contact_email"] = email
                prospects.append(p)
        # Se non ha email reale e non ha sito, salta

    added = 0
    for p in prospects:
        try:
            existing = db.table("prospects").select("id").eq("company_name", p["company_name"]).execute()
            if not existing.data:
                db.table("prospects").insert(p).execute()
                added += 1
        except:
            try:
                db.table("prospects").insert(p).execute()
                added += 1
            except:
                pass

    try:
        existing = db.table("markets").select("*").eq("sector", sector).execute()
        if existing.data:
            db.table("markets").update({"leads_found": existing.data[0]["leads_found"] + added}).eq("sector", sector).execute()
        else:
            db.table("markets").insert({"sector": sector, "score": 50, "leads_found": added}).execute()
    except:
        pass

    print(f"Trovati {len(raw_results)} da Maps, email reali: {len(prospects)}, salvati: {added}")
    return {"prospects_found": added, "sector": sector, "location": location, "source": "apify"}

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

def _track_cost(db, euros, description=""):
    try:
        existing = db.table("settings").select("value").eq("key", "budget_usato").execute()
        current = float(existing.data[0]["value"]) if existing.data else 0.0
        new_total = round(current + euros, 4)
        if existing.data:
            db.table("settings").update({"value": str(new_total)}).eq("key", "budget_usato").execute()
        else:
            db.table("settings").insert({"key": "budget_usato", "value": str(new_total)}).execute()
    except Exception as e:
        print(f"Cost tracking error: {e}")

def generate_email(db, params):
    import anthropic
    import smtplib
    from email.mime.text import MIMEText
    claude = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
    prospect = params.get("prospect", {})
    company = prospect.get("company_name", "cliente")
    sector = prospect.get("sector", "azienda")
    email_to = prospect.get("contact_email", "")

    # Use CEO-optimized prompt if available
    try:
        setting = db.table("settings").select("value").eq("key", "email_prompt").execute()
        if setting.data:
            prompt = setting.data[0]["value"].replace("[company]", company).replace("[sector]", sector)
        else:
            raise Exception("no setting")
    except Exception:
        prompt = f"Scrivi email vendita per: {company}, settore: {sector}. Servizio: Agente AI per acquisizione clienti. Includi CTA per demo gratuita. Max 120 parole."

    response = claude.messages.create(model="claude-haiku-4-5-20251001", max_tokens=200, messages=[{"role": "user", "content": prompt}])
    email_body = response.content[0].text

    # Track Claude cost (Haiku ~$0.80/MTok in, $4/MTok out → EUR ×0.92)
    cost_eur = (len(prompt) / 4 * 0.80 + len(email_body) / 4 * 4.0) / 1_000_000 * 0.92
    _track_cost(db, cost_eur, f"email {company}")

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

    follow_up_date = (date.today() + timedelta(days=3)).isoformat()
    db.table("prospects").update({
        "status": "contacted",
        "agent_notes": email_body[:100],
        "follow_up_at": follow_up_date
    }).eq("id", prospect.get("id")).execute()
    return {"email": email_body[:100], "sent": sent, "prospect": company}


def check_email_replies(db):
    """Scan Zoho IMAP inbox for replies from contacted prospects. Returns count of new replies."""
    import imaplib
    import email as email_lib

    EMAIL = os.getenv("EMAIL_ADDRESS")
    EMAIL_PASS = os.getenv("EMAIL_PASSWORD")
    if not EMAIL or not EMAIL_PASS:
        return 0
    try:
        mail = imaplib.IMAP4_SSL("imap.zoho.eu", 993)
        mail.login(EMAIL, EMAIL_PASS)
        mail.select("INBOX")
        _, msgs = mail.search(None, "UNSEEN")
        if not msgs[0]:
            mail.logout()
            return 0

        contacted = db.table("prospects").select("id,contact_email,company_name,sector").in_(
            "status", ["contacted", "followup_1", "followup_2"]
        ).execute()
        prospect_map = {
            (p.get("contact_email") or "").lower().strip(): p
            for p in (contacted.data or [])
            if p.get("contact_email")
        }

        replied_count = 0
        for num in msgs[0].split():
            try:
                _, data = mail.fetch(num, "(RFC822)")
                msg = email_lib.message_from_bytes(data[0][1])
                from_header = msg.get("From", "")
                found_emails = re.findall(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', from_header)
                for sender in found_emails:
                    p = prospect_map.get(sender.lower())
                    if p:
                        db.table("prospects").update({"status": "replied"}).eq("id", p["id"]).execute()
                        send_telegram(
                            f"📬 RISPOSTA ricevuta!\n"
                            f"Azienda: {p.get('company_name')}\n"
                            f"Email: {sender}\nSettore: {p.get('sector')}"
                        )
                        replied_count += 1
                        break
            except Exception:
                continue

        mail.logout()
        return replied_count
    except Exception as e:
        print(f"IMAP check error: {e}")
        return 0


def send_followups(db):
    """Send scheduled follow-up emails (day+3, day+7) to non-replying prospects. Returns count sent."""
    import anthropic
    import smtplib
    from email.mime.text import MIMEText

    today = date.today().isoformat()
    EMAIL = os.getenv("EMAIL_ADDRESS")
    EMAIL_PASS = os.getenv("EMAIL_PASSWORD")

    try:
        due = db.table("prospects").select("*").in_("status", ["contacted", "followup_1"]).lte("follow_up_at", today).limit(10).execute()
    except Exception as e:
        print(f"Follow-up query error: {e}")
        return 0

    if not due.data:
        return 0

    sent = 0
    claude_client = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))

    for p in due.data:
        status = p.get("status", "contacted")
        company = p.get("company_name", "")
        email_to = p.get("contact_email", "")
        sector = p.get("sector", "")
        if not email_to:
            continue
        try:
            if status == "contacted":
                prompt = (
                    f"Follow-up email brevissima (max 60 parole) per {company} ({sector}). "
                    f"Tono amichevole, nessuna pressione. Chiedi solo se hanno avuto modo di leggere "
                    f"la proposta precedente sull'agente AI. CTA: risposta sì/no. Firma: Team GetAutomatik"
                )
                new_status = "followup_1"
                next_date = (date.today() + timedelta(days=4)).isoformat()
                subject = f"Re: Agente AI per {company}"
            else:
                prompt = (
                    f"Email finale (max 50 parole) per {company} ({sector}). "
                    f"Ultima email, poi non scriverò più. Offri 30 minuti di demo gratuita senza impegno. "
                    f"Firma: Team GetAutomatik"
                )
                new_status = "followup_2"
                next_date = None
                subject = f"Demo gratuita per {company} — ultima opportunità"

            response = claude_client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=150,
                messages=[{"role": "user", "content": prompt}]
            )
            body = response.content[0].text
            cost_eur = (len(prompt) / 4 * 0.80 + len(body) / 4 * 4.0) / 1_000_000 * 0.92
            _track_cost(db, cost_eur, f"followup {company}")

            if EMAIL and EMAIL_PASS:
                msg = MIMEText(body)
                msg["Subject"] = subject
                msg["From"] = EMAIL
                msg["To"] = email_to
                with smtplib.SMTP_SSL("smtp.zoho.eu", 465) as server:
                    server.login(EMAIL, EMAIL_PASS)
                    server.send_message(msg)
                update = {"status": new_status}
                if next_date:
                    update["follow_up_at"] = next_date
                db.table("prospects").update(update).eq("id", p["id"]).execute()
                sent += 1
                print(f"Follow-up {new_status} → {company} ({email_to})")
        except Exception as e:
            print(f"Follow-up error for {company}: {e}")

    return sent


def ceo_optimize_emails(db):
    """CEO analyzes reply rates and rewrites the email generation prompt using Claude. Returns new prompt or None."""
    import anthropic

    try:
        all_contacted = db.table("prospects").select("id").in_(
            "status", ["contacted", "followup_1", "followup_2", "replied", "converted"]
        ).execute()
        total_contacted = len(all_contacted.data) if all_contacted.data else 0

        if total_contacted < 10:
            print(f"CEO optimize: {total_contacted} contatti, minimo 10 richiesto")
            return None

        replied_rows = db.table("prospects").select("agent_notes,sector,company_name").eq("status", "replied").execute()
        total_replied = len(replied_rows.data) if replied_rows.data else 0
        reply_rate = total_replied / total_contacted * 100

        try:
            s = db.table("settings").select("value").eq("key", "email_prompt").execute()
            current_prompt = s.data[0]["value"] if s.data else "Scrivi email vendita per: [company], settore: [sector]. Servizio: Agente AI per acquisizione clienti. Includi CTA per demo gratuita. Max 120 parole."
        except Exception:
            current_prompt = "Scrivi email vendita per: [company], settore: [sector]. Max 120 parole."

        examples = "\n".join(
            f"- {r.get('company_name')} ({r.get('sector')}): {r.get('agent_notes', '')}"
            for r in (replied_rows.data or [])[:5] if r.get("agent_notes")
        ) or "Nessuna risposta ancora"

        meta_prompt = (
            f"Sei il CEO di GetAutomatik AI.\n\n"
            f"Email inviate: {total_contacted} | Risposte: {total_replied} | Reply rate: {reply_rate:.1f}%\n\n"
            f"Prompt attuale:\n\"{current_prompt}\"\n\n"
            f"Email che hanno ricevuto risposta:\n{examples}\n\n"
            f"Riscrivi il prompt di generazione email per massimizzare le risposte nel mercato italiano B2B. "
            f"Usa [company] e [sector] come placeholder. "
            f"Rispondi SOLO con il nuovo prompt ottimizzato, niente altro."
        )

        claude_client = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
        response = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=300,
            messages=[{"role": "user", "content": meta_prompt}]
        )
        new_prompt = response.content[0].text.strip()

        try:
            existing = db.table("settings").select("id").eq("key", "email_prompt").execute()
            if existing.data:
                db.table("settings").update({"value": new_prompt}).eq("key", "email_prompt").execute()
            else:
                db.table("settings").insert({"key": "email_prompt", "value": new_prompt}).execute()
        except Exception as e:
            print(f"CEO optimize save error: {e}")

        try:
            db.table("decisions").insert({
                "agent_name": "CEO",
                "thought_process": f"Reply rate: {reply_rate:.1f}% su {total_contacted} email",
                "decision": "OTTIMIZZA_PROMPT_EMAIL",
                "action_taken": "Prompt riscritto da Claude",
                "result": new_prompt[:300]
            }).execute()
        except Exception:
            pass

        send_telegram(
            f"🧠 CEO ha ottimizzato il prompt email\n"
            f"Reply rate: {reply_rate:.1f}% ({total_replied}/{total_contacted})\n"
            f"Nuovo prompt: {new_prompt[:100]}..."
        )
        return new_prompt
    except Exception as e:
        print(f"CEO optimize error: {e}")
        return None

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
def find_email_from_website(db, params):
    import requests as r
    import re
    company = params.get("company_name", "")
    if not company:
        return {"email": None, "found": False}
    try:
        query = f"{company} email contatti"
        resp = r.get(f"https://www.google.com/search?q={query.replace(' ', '+')}", 
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', resp.text)
        if emails:
            business_emails = [e for e in emails if not any(x in e.lower() for x in ['gmail', 'yahoo', 'hotmail', 'example'])]
            if business_emails:
                return {"email": business_emails[0], "found": True}
    except:
        pass
    return {"email": None, "found": False}

def switch_market(db, params):
    new_sector = params.get("sector", "")
    old_sector = params.get("old_sector", "")
    if not new_sector:
        return {"error": "Nessun mercato specificato"}
    # Attiva nuovo mercato
    existing = db.table("markets").select("*").eq("sector", new_sector).execute()
    if not existing.data:
        db.table("markets").insert({"sector": new_sector, "score": 50, "leads_found": 0}).execute()
        db.table("decisions").insert({
            "agent_name": "CEO",
            "thought_process": f"Switch mercato",
            "decision": "CAMBIARE_MERCATO",
            "action_taken": f"Da {old_sector} a {new_sector}",
            "result": "Nuovo mercato attivato"
        }).execute()
        send_telegram(f"🔄 SWITCH MERCATO: da {old_sector} a {new_sector}")
        return {"switched": True, "new_sector": new_sector}
    return {"switched": False, "reason": "Mercato già esistente"}
