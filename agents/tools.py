import os, json, requests as req, time, re, random
from datetime import date, timedelta
from dotenv import load_dotenv
load_dotenv()
from supabase import create_client

DAILY_EMAIL_LIMIT = 30
CITIES = ["Milano", "Roma", "Torino", "Napoli", "Bologna", "Firenze", "Venezia", "Bari", "Palermo", "Genova"]

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

def _get_company_context(website):
    """Scrape homepage title + meta description for email personalization."""
    if not website:
        return ""
    try:
        resp = req.get(website, timeout=6, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
        html = resp.text[:20000]
        title = re.search(r'<title[^>]*>([^<]{3,80})</title>', html, re.I)
        desc = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{10,200})', html, re.I)
        parts = []
        if title:
            parts.append(title.group(1).strip())
        if desc:
            parts.append(desc.group(1).strip())
        return " — ".join(parts)[:180]
    except Exception:
        return ""

def _check_and_increment_daily_emails(db):
    """Returns True if under daily limit, increments counter."""
    key = f"daily_emails_{date.today().isoformat()}"
    try:
        existing = db.table("settings").select("value").eq("key", key).execute()
        if existing.data:
            count = int(existing.data[0]["value"])
            if count >= DAILY_EMAIL_LIMIT:
                return False
            db.table("settings").update({"value": str(count + 1)}).eq("key", key).execute()
        else:
            db.table("settings").insert({"key": key, "value": "1"}).execute()
        return True
    except Exception:
        return True

def _rotation_city():
    return CITIES[date.today().timetuple().tm_yday % len(CITIES)]

def _send_plain_email(to_email, subject, body):
    """Send plain-text email with proper deliverability headers."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    EMAIL = os.getenv("EMAIL_ADDRESS")
    EMAIL_PASS = os.getenv("EMAIL_PASSWORD")
    if not EMAIL or not EMAIL_PASS or not to_email:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"GetAutomatik AI <{EMAIL}>"
        msg["To"] = to_email
        msg["Reply-To"] = EMAIL
        msg["List-Unsubscribe"] = f"<mailto:{EMAIL}?subject=unsubscribe>"
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP_SSL("smtp.zoho.eu", 465) as server:
            server.login(EMAIL, EMAIL_PASS)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Email send error: {e}")
        return False

def _check_converted(db, email):
    """True if this email already has an active/trial client record."""
    try:
        r = db.table("clients").select("status").eq("contact_email", email).execute()
        return bool(r.data) and r.data[0].get("status") in ("active", "trial")
    except Exception:
        return False

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
    location = params.get("location") or _rotation_city()
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

    # Track Apify costs (~€0.05/Maps run + €0.01/contact scrape)
    maps_cost = 0.05 if raw_results else 0.0
    scrape_cost = 0.01 * sum(1 for p in raw_results if not _is_real_email(p["contact_email"]) and p["website"])
    _track_cost(db, maps_cost + scrape_cost, f"apify {sector} {location}")

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

def _handle_reply(db, prospect, body_text):
    """Classify reply with Claude, auto-respond, push warm prospects toward checkout."""
    import anthropic

    company = prospect.get("company_name", "")
    email_to = prospect.get("contact_email", "")
    sector = prospect.get("sector", "")
    CALENDLY = os.getenv("CALENDLY_URL", "https://calendly.com/getautomatik")
    CHECKOUT = os.getenv("CHECKOUT_URL", "https://getautomatik.com/checkout")

    claude_client = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))

    clf = claude_client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=10,
        messages=[{"role": "user", "content":
            f"Classifica questa risposta email di un'azienda italiana ({company}, settore {sector}) "
            f"a una proposta di agente AI:\n\"{body_text[:400]}\"\n\n"
            f"Rispondi SOLO con: INTERESTED / NOT_INTERESTED / QUESTION / OUT_OF_OFFICE"
        }]
    ).content[0].text.strip().upper()

    if "INTERESTED" in clf:
        # Rich closing email: value prop + two CTAs (checkout direct + Calendly)
        body = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=350,
            messages=[{"role": "user", "content":
                f"Scrivi una email di chiusura vendita (120 parole max) per {company} ({sector}) "
                f"che ha mostrato interesse al nostro agente AI.\n"
                f"Struttura:\n"
                f"1. Apertura calda (1 frase personalizzata per {sector})\n"
                f"2. Cosa ottengono: l'agente trova e contatta automaticamente clienti qualificati ogni giorno\n"
                f"3. Due opzioni chiare:\n"
                f"   → Inizia il trial gratuito 7 giorni ora: {CHECKOUT}\n"
                f"   → Preferisci una call di 30 min? {CALENDLY}\n"
                f"4. P.S. breve: entro 24h dall'attivazione l'agente è operativo\n"
                f"Tono: diretto, caldo, zero pressione. Firma: Team GetAutomatik"
            }]
        ).content[0].text

        _send_plain_email(email_to, f"Come funziona per {company} — prossimo passo", body)

        warm_date = (date.today() + timedelta(days=2)).isoformat()
        db.table("prospects").update({
            "status": "warm_1",
            "follow_up_at": warm_date,
            "agent_notes": f"INTERESTED: {body_text[:120]}"
        }).eq("id", prospect["id"]).execute()
        send_telegram(
            f"🔥 PROSPECT CALDO → warm sequence avviata!\n"
            f"{company} ({sector})\nEmail: {email_to}\n"
            f"Closing email inviata con checkout + Calendly"
        )

    elif "QUESTION" in clf:
        body = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=250,
            messages=[{"role": "user", "content":
                f"Un prospect italiano ({company}, {sector}) ha fatto questa domanda:\n\"{body_text[:300]}\"\n"
                f"Rispondi in modo convincente e diretto (max 90 parole). "
                f"Servizio: agente AI che trova e contatta clienti automaticamente, 197€/mese, 7gg trial gratuito.\n"
                f"Chiudi con due opzioni: trial {CHECKOUT} oppure call {CALENDLY}\n"
                f"Firma: Team GetAutomatik"
            }]
        ).content[0].text
        _send_plain_email(email_to, f"Re: Agente AI per {company}", body)
        db.table("prospects").update({
            "status": "warm_1",
            "follow_up_at": (date.today() + timedelta(days=3)).isoformat(),
            "agent_notes": f"QUESTION: {body_text[:120]}"
        }).eq("id", prospect["id"]).execute()
        send_telegram(f"❓ Domanda da {company}\n{body_text[:100]}\nRisposta + CTA inviati → warm_1")

    elif "NOT_INTERESTED" in clf:
        db.table("prospects").update({
            "status": "dead",
            "agent_notes": f"NOT_INTERESTED: {body_text[:120]}"
        }).eq("id", prospect["id"]).execute()
        send_telegram(f"🚫 {company} non interessato → dead")

    elif "OUT_OF_OFFICE" in clf:
        db.table("prospects").update({
            "status": "contacted",
            "follow_up_at": (date.today() + timedelta(days=5)).isoformat(),
            "agent_notes": "OUT_OF_OFFICE — ricontatto +5gg"
        }).eq("id", prospect["id"]).execute()
        send_telegram(f"🏖️ {company} fuori ufficio → ricontatto +5gg")

    _track_cost(db, 0.0006, f"reply handler {company}")


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
    website = prospect.get("website", "")

    # Rate limit: max DAILY_EMAIL_LIMIT emails/day to protect deliverability
    if not _check_and_increment_daily_emails(db):
        print(f"Daily email limit reached ({DAILY_EMAIL_LIMIT}), skipping {company}")
        return {"email": "", "sent": False, "prospect": company, "skipped": "daily_limit"}

    # Personalization: scrape homepage for company context
    company_context = _get_company_context(website) if website else ""

    # Use CEO-optimized prompt if available
    try:
        setting = db.table("settings").select("value").eq("key", "email_prompt").execute()
        if setting.data:
            prompt = setting.data[0]["value"].replace("[company]", company).replace("[sector]", sector)
        else:
            raise Exception("no setting")
    except Exception:
        prompt = (
            f"Scrivi email fredda di vendita per: {company}, settore: {sector}. "
            f"Servizio: Agente AI che trova e contatta clienti in automatico, 197€/mese, 7 giorni trial gratuito. "
            f"Includi CTA per demo gratuita. Max 120 parole. Italiano."
        )

    if company_context:
        prompt += f"\nContesto azienda (da usare per personalizzare): {company_context}"

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=220,
        messages=[{"role": "user", "content": prompt}]
    )
    email_body = response.content[0].text

    cost_eur = (len(prompt) / 4 * 0.80 + len(email_body) / 4 * 4.0) / 1_000_000 * 0.92
    _track_cost(db, cost_eur, f"email {company}")

    sent = _send_plain_email(email_to, f"Automatizza la crescita di {company}", email_body)

    follow_up_date = (date.today() + timedelta(days=3)).isoformat()
    db.table("prospects").update({
        "status": "contacted",
        "agent_notes": email_body[:100],
        "follow_up_at": follow_up_date
    }).eq("id", prospect.get("id")).execute()
    return {"email": email_body[:100], "sent": sent, "prospect": company}


def check_email_replies(db):
    """Scan Zoho IMAP for replies from contacted prospects, classify and auto-respond."""
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
            "status", ["contacted", "followup_1"]
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

                # Extract plain text body
                body_text = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            try:
                                body_text = part.get_payload(decode=True).decode(errors="ignore")
                                break
                            except Exception:
                                pass
                else:
                    try:
                        body_text = msg.get_payload(decode=True).decode(errors="ignore")
                    except Exception:
                        pass
                body_text = body_text[:1200].strip()

                from_header = msg.get("From", "")
                found_emails = re.findall(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', from_header)
                for sender in found_emails:
                    p = prospect_map.get(sender.lower())
                    if p:
                        # Classify reply and auto-respond
                        _handle_reply(db, p, body_text)
                        replied_count += 1
                        break
            except Exception as e:
                print(f"IMAP parse error: {e}")
                continue

        mail.logout()
        return replied_count
    except Exception as e:
        print(f"IMAP check error: {e}")
        return 0


def send_followups(db):
    """Handle all scheduled sequences: cold follow-ups + warm closing sequence."""
    import anthropic

    today = date.today().isoformat()
    CHECKOUT = os.getenv("CHECKOUT_URL", "https://getautomatik.com/checkout")
    CALENDLY = os.getenv("CALENDLY_URL", "https://calendly.com/getautomatik")

    try:
        due = db.table("prospects").select("*").in_(
            "status", ["contacted", "followup_1", "warm_1", "warm_2"]
        ).lte("follow_up_at", today).limit(15).execute()
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
            # --- Cold follow-up sequence ---
            if status == "contacted":
                prompt = (
                    f"Follow-up email brevissima (max 60 parole) per {company} ({sector}). "
                    f"Amichevole, zero pressione. Chiedi se hanno avuto modo di leggere la proposta "
                    f"sull'agente AI. CTA: risposta sì/no. Firma: Team GetAutomatik"
                )
                new_status = "followup_1"
                next_date = (date.today() + timedelta(days=4)).isoformat()
                subject = f"Hai 2 minuti, {company}?"

            elif status == "followup_1":
                prompt = (
                    f"Email finale (max 55 parole) per {company} ({sector}). "
                    f"Ultima email, poi non scriverò più. Offri trial gratuito 7 giorni: {CHECKOUT} "
                    f"Firma: Team GetAutomatik"
                )
                new_status = "dead"
                next_date = None
                subject = f"Ultima proposta per {company}"

            # --- Warm closing sequence (prospect ha già risposto con interesse) ---
            elif status == "warm_1":
                if _check_converted(db, email_to):
                    db.table("prospects").update({"status": "converted"}).eq("id", p["id"]).execute()
                    continue
                prompt = (
                    f"Follow-up caldo (max 70 parole) per {company} ({sector}) che aveva mostrato interesse "
                    f"ma non ha ancora attivato il trial. Ricorda il valore, zero pressione. "
                    f"Link diretto: {CHECKOUT} Firma: Team GetAutomatik"
                )
                new_status = "warm_2"
                next_date = (date.today() + timedelta(days=3)).isoformat()
                subject = f"{company} — il trial è ancora disponibile"

            elif status == "warm_2":
                if _check_converted(db, email_to):
                    db.table("prospects").update({"status": "converted"}).eq("id", p["id"]).execute()
                    continue
                prompt = (
                    f"Email di chiusura finale (max 60 parole) per {company} ({sector}). "
                    f"Offri 14 giorni di trial gratuito invece di 7 — offerta valida solo questa settimana. "
                    f"Link: {CHECKOUT} Alternativa call: {CALENDLY} "
                    f"Firma: Team GetAutomatik"
                )
                new_status = "warm_closed"
                next_date = None
                subject = f"14 giorni gratis per {company} — offerta a tempo"

            else:
                continue

            response = claude_client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=160,
                messages=[{"role": "user", "content": prompt}]
            )
            body = response.content[0].text
            cost_eur = (len(prompt) / 4 * 0.80 + len(body) / 4 * 4.0) / 1_000_000 * 0.92
            _track_cost(db, cost_eur, f"{status} {company}")

            if _send_plain_email(email_to, subject, body):
                update = {"status": new_status}
                if next_date:
                    update["follow_up_at"] = next_date
                db.table("prospects").update(update).eq("id", p["id"]).execute()
                sent += 1
                print(f"{status} → {new_status}: {company}")
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
