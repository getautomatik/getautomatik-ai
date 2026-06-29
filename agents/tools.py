import os, json, requests as req, time, re, random
from datetime import date, timedelta, datetime
from dotenv import load_dotenv
load_dotenv()

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
        body = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=300,
            messages=[{"role": "user", "content":
                f"Scrivi una email di chiusura vendita (100 parole max) per {company} ({sector}) "
                f"che ha mostrato interesse al nostro agente AI.\n"
                f"1. Frase di apertura personalizzata per il settore {sector}\n"
                f"2. Cosa ottengono: l'agente trova e contatta clienti qualificati ogni giorno in automatico\n"
                f"3. CTA unico e diretto: inizia il trial gratuito di 7 giorni → {CHECKOUT}\n"
                f"4. P.S.: entro 24h dall'attivazione l'agente è operativo\n"
                f"Tono: diretto, caldo, zero pressione. Firma: Team GetAutomatik"
            }]
        ).content[0].text

        _send_plain_email(email_to, f"Come funziona per {company} — inizia gratis", body)

        warm_date = (date.today() + timedelta(days=2)).isoformat()
        db.table("prospects").update({
            "status": "warm_1",
            "follow_up_at": warm_date,
            "agent_notes": f"INTERESTED: {body_text[:120]}"
        }).eq("id", prospect["id"]).execute()
        send_telegram(
            f"🔥 PROSPECT CALDO → warm sequence!\n"
            f"{company} ({sector}) — {email_to}\n"
            f"Closing email inviata → trial {CHECKOUT}"
        )

    elif "QUESTION" in clf:
        body = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=250,
            messages=[{"role": "user", "content":
                f"Un prospect italiano ({company}, {sector}) ha fatto questa domanda:\n\"{body_text[:300]}\"\n"
                f"Rispondi in modo convincente e diretto (max 80 parole). "
                f"Servizio: agente AI che trova e contatta clienti automaticamente, 197€/mese, 7gg trial gratuito.\n"
                f"Chiudi con CTA diretto: prova gratis → {CHECKOUT}\n"
                f"Firma: Team GetAutomatik"
            }]
        ).content[0].text
        _send_plain_email(email_to, f"Re: Agente AI per {company}", body)
        db.table("prospects").update({
            "status": "warm_1",
            "follow_up_at": (date.today() + timedelta(days=3)).isoformat(),
            "agent_notes": f"QUESTION: {body_text[:120]}"
        }).eq("id", prospect["id"]).execute()
        send_telegram(f"❓ Domanda da {company} → risposta + trial link inviati")

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
        send_telegram(f"🏖ï¸ {company} fuori ufficio → ricontatto +5gg")

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
                    f"Link diretto: {CHECKOUT} "
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

# --- Revenue pipeline: deterministic micro-agency flow ---
# AI is used only for website analysis, synthesis, copywriting and reply classification.

DENTIST_VERTICAL = "dentisti"
AUDIT_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")


def _today_key():
    return date.today().isoformat()


def _safe_int(value, default=0):
    try:
        return int(value or default)
    except Exception:
        return default


def _normalize_url(url):
    if not url:
        return ""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _extract_domain(url):
    try:
        clean = _normalize_url(url)
        return re.sub(r"^www\.", "", clean.split("//", 1)[-1].split("/", 1)[0].lower())
    except Exception:
        return ""


def _metric_increment(db, field, amount=1):
    today = _today_key()
    allowed = {"emails_sent", "replies", "calls_booked", "clients_closed", "mrr", "leads_found"}
    if field not in allowed:
        return False
    try:
        existing = db.table("daily_metrics").select("*").eq("date", today).execute()
        if existing.data:
            row = existing.data[0]
            current = _safe_int(row.get(field), 0)
            db.table("daily_metrics").update({field: current + amount}).eq("date", today).execute()
        else:
            payload = {
                "date": today,
                "leads_found": 0,
                "emails_sent": 0,
                "replies": 0,
                "calls_booked": 0,
                "clients_closed": 0,
                "mrr": 0,
            }
            payload[field] = amount
            db.table("daily_metrics").insert(payload).execute()
        return True
    except Exception as e:
        print(f"daily_metrics update skipped: {e}")
        return False


class WebsiteAuditAgent:
    """Audits a prospect website and returns commercial problems/opportunities."""

    def __init__(self):
        self.api_key = os.getenv("CLAUDE_API_KEY")

    def audit(self, website_url, company_name=""):
        website_url = _normalize_url(website_url)
        fallback_name = company_name or _extract_domain(website_url) or "Studio dentistico"
        if not website_url:
            return {
                "company_name": fallback_name,
                "problems": ["Sito web non disponibile o non trovato nella fonte lead"],
                "opportunities": ["Recuperare un contatto diretto prima di inviare outreach"],
                "estimated_lost_leads": 0,
                "score": 10,
            }

        html = ""
        status_code = None
        try:
            resp = req.get(website_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
            status_code = resp.status_code
            html = resp.text[:35000]
        except Exception as e:
            return {
                "company_name": fallback_name,
                "problems": [f"Homepage non raggiungibile durante audit: {type(e).__name__}"],
                "opportunities": ["Contattare lo studio con un audit di presenza digitale e risposta lead"],
                "estimated_lost_leads": 3,
                "score": 20,
            }

        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()[:5000]
        title = ""
        title_match = re.search(r"<title[^>]*>([^<]{3,120})</title>", html, re.I)
        if title_match:
            title = title_match.group(1).strip()

        has_booking = bool(re.search(r"prenota|booking|calendly|appuntamento|visita", html, re.I))
        has_phone = bool(re.search(r"(\+39|tel:|\b0\d{1,3}[\s.-]?\d{5,}\b)", html, re.I))
        has_whatsapp = "whatsapp" in html.lower() or "wa.me" in html.lower()
        has_contact_form = bool(re.search(r"<form|contattaci|richiedi", html, re.I))
        heuristic_score = 55
        heuristic_score += 10 if has_booking else -12
        heuristic_score += 8 if has_phone else -8
        heuristic_score += 6 if has_whatsapp else -4
        heuristic_score += 8 if has_contact_form else -8
        heuristic_score = max(5, min(95, heuristic_score))

        if self.api_key:
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=self.api_key)
                prompt = (
                    "Analizza questo sito di uno studio dentistico italiano per una micro-agenzia AI. "
                    "Usa solo il contenuto fornito. Trova problemi che fanno perdere richieste pazienti "
                    "e opportunita per recuperare richieste e riempire calendario. "
                    "Rispondi SOLO JSON con chiavi: company_name, problems (array), opportunities (array), "
                    "estimated_lost_leads (integer mensile), score (0-100).\n\n"
                    f"URL: {website_url}\nTitolo: {title}\nHTTP: {status_code}\n"
                    f"Segnali: booking={has_booking}, phone={has_phone}, whatsapp={has_whatsapp}, form={has_contact_form}\n"
                    f"Testo sito:\n{text[:3500]}"
                )
                response = client.messages.create(
                    model=AUDIT_MODEL,
                    max_tokens=450,
                    messages=[{"role": "user", "content": prompt}],
                )
                data = json.loads(response.content[0].text)
                return {
                    "company_name": data.get("company_name") or fallback_name,
                    "problems": list(data.get("problems") or [])[:5],
                    "opportunities": list(data.get("opportunities") or [])[:5],
                    "estimated_lost_leads": _safe_int(data.get("estimated_lost_leads"), 0),
                    "score": max(0, min(100, _safe_int(data.get("score"), heuristic_score))),
                }
            except Exception as e:
                print(f"Website AI audit fallback: {e}")

        problems = []
        opportunities = []
        if not has_booking:
            problems.append("Manca una prenotazione online evidente per i nuovi pazienti")
            opportunities.append("Inserire una CTA di prenotazione e follow-up automatico per richieste fuori orario")
        if not has_whatsapp:
            problems.append("WhatsApp non e visibile come canale rapido per richieste urgenti")
            opportunities.append("Aggiungere risposta automatica WhatsApp/email per richieste di prima visita")
        if not has_contact_form:
            problems.append("Il form contatti non e immediatamente riconoscibile")
            opportunities.append("Ridurre attrito con form breve e notifica immediata allo studio")
        if not problems:
            problems.append("Il sito ha canali di contatto, ma non mostra un recupero automatico delle richieste perse")
            opportunities.append("Automatizzare richiamo e qualificazione delle richieste entro pochi minuti")

        lost = max(2, round((100 - heuristic_score) / 10))
        return {
            "company_name": fallback_name,
            "problems": problems[:5],
            "opportunities": opportunities[:5],
            "estimated_lost_leads": lost,
            "score": heuristic_score,
        }


class LeadScoring:
    """Deterministic score: no AI decision making."""

    def score(self, prospect, audit):
        score = 0
        if prospect.get("website"):
            score += 25
        if _is_real_email(prospect.get("contact_email")):
            score += 30
        if (prospect.get("sector") or "").lower() in ("dentisti", "dentista", "studio dentistico"):
            score += 15
        if audit.get("estimated_lost_leads", 0) >= 4:
            score += 15
        if audit.get("score", 0) < 65:
            score += 10
        if prospect.get("contact_phone"):
            score += 5
        reason = "email business + sito + audit dentisti" if score >= 65 else "lead debole o audit poco urgente"
        return {"score": min(100, score), "qualified": score >= 65, "reason": reason}


class OutreachGenerator:
    """Creates audit-led outreach copy for dentists."""

    def __init__(self):
        self.api_key = os.getenv("CLAUDE_API_KEY")

    def generate(self, prospect, audit, score):
        company = prospect.get("company_name") or audit.get("company_name") or "Studio"
        first_problem = (audit.get("problems") or ["alcune richieste pazienti possono perdersi fuori orario"])[0]
        lost = audit.get("estimated_lost_leads", 3)
        if self.api_key:
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=self.api_key)
                prompt = (
                    "Scrivi una email cold B2B in italiano per uno studio dentistico. "
                    "Obiettivo: offrire audit gratuito, non vendere subito abbonamento. Max 110 parole. "
                    "Tono: diretto, concreto, professionale. CTA: Ricevi audit gratuito. "
                    "Non inventare dati oltre quelli forniti.\n\n"
                    f"Studio: {company}\nProblema rilevato: {first_problem}\n"
                    f"Richieste potenzialmente perse/mese: {lost}\nScore lead: {score.get('score')}\n"
                )
                response = client.messages.create(
                    model=AUDIT_MODEL,
                    max_tokens=240,
                    messages=[{"role": "user", "content": prompt}],
                )
                body = response.content[0].text.strip()
            except Exception as e:
                print(f"Outreach AI fallback: {e}")
                body = ""
        else:
            body = ""

        if not body:
            body = (
                f"Buongiorno, ho analizzato il sito di {company} e ho notato questo punto: {first_problem}.\n\n"
                f"Per studi dentistici questo spesso significa richieste pazienti non recuperate, soprattutto fuori orario. "
                f"Abbiamo preparato un audit gratuito con 3 azioni pratiche per recuperare richieste perse e riempire meglio il calendario.\n\n"
                "Vuole che glielo invii?\n\nTeam GetAutomatik"
            )
        return {
            "subject": f"Audit gratuito per {company}",
            "body": body,
            "cta": "Ricevi audit gratuito",
        }


class CampaignSender:
    """Sends queued outreach with existing SMTP and logs to Supabase."""

    def queue(self, db, prospect, audit, scoring, message):
        payload = {
            "prospect_id": prospect.get("id"),
            "company_name": prospect.get("company_name") or audit.get("company_name"),
            "contact_email": prospect.get("contact_email"),
            "sector": prospect.get("sector") or DENTIST_VERTICAL,
            "status": "queued",
            "score": scoring.get("score"),
            "subject": message.get("subject"),
            "body": message.get("body"),
            "audit": json.dumps(audit, ensure_ascii=False),
            "created_at": datetime_now_iso(),
        }
        try:
            existing = db.table("outreach").select("id,status").eq("contact_email", payload["contact_email"]).execute()
            if existing.data:
                return {"queued": False, "reason": "duplicate_outreach"}
            db.table("outreach").insert(payload).execute()
            return {"queued": True}
        except Exception as e:
            print(f"outreach queue skipped: {e}")
            return {"queued": False, "reason": str(e)[:120]}

    def send_queued(self, db, limit=DAILY_EMAIL_LIMIT):
        try:
            rows = db.table("outreach").select("*").eq("status", "queued").limit(limit).execute()
        except Exception as e:
            print(f"outreach table unavailable: {e}")
            return 0
        sent = 0
        for row in rows.data or []:
            email_to = row.get("contact_email")
            if not _is_real_email(email_to):
                db.table("outreach").update({"status": "skipped", "error": "invalid_email"}).eq("id", row.get("id")).execute()
                continue
            if not _check_and_increment_daily_emails(db):
                break
            ok = _send_plain_email(email_to, row.get("subject", "Audit gratuito"), row.get("body", ""))
            status = "sent" if ok else "failed"
            update = {"status": status, "sent_at": datetime_now_iso()}
            if not ok:
                update["error"] = "smtp_failed"
            try:
                db.table("outreach").update(update).eq("id", row.get("id")).execute()
                if row.get("prospect_id"):
                    db.table("prospects").update({"status": "contacted", "follow_up_at": (date.today() + timedelta(days=3)).isoformat()}).eq("id", row.get("prospect_id")).execute()
            except Exception as e:
                print(f"outreach status update failed: {e}")
            if ok:
                _metric_increment(db, "emails_sent", 1)
                sent += 1
        return sent


def datetime_now_iso():
    from datetime import datetime
    return datetime.utcnow().isoformat() + "Z"


class MetricsTracker:
    def snapshot(self, db):
        today = _today_key()
        result = {"leads_today": 0, "emails_sent": 0, "replies": 0, "calls_booked": 0, "clients_closed": 0, "mrr": 0}
        try:
            metrics = db.table("daily_metrics").select("*").eq("date", today).execute()
            if metrics.data:
                row = metrics.data[0]
                result.update({
                    "leads_today": _safe_int(row.get("leads_found"), 0),
                    "emails_sent": _safe_int(row.get("emails_sent"), 0),
                    "replies": _safe_int(row.get("replies"), 0),
                    "calls_booked": _safe_int(row.get("calls_booked"), 0),
                    "clients_closed": _safe_int(row.get("clients_closed"), 0),
                    "mrr": _safe_int(row.get("mrr"), 0),
                })
        except Exception as e:
            print(f"daily_metrics read fallback: {e}")
        try:
            clients = db.table("clients").select("mrr").eq("status", "active").execute()
            result["clients_closed"] = len(clients.data or [])
            result["mrr"] = sum(_safe_int(c.get("mrr"), 0) for c in (clients.data or []))
        except Exception:
            pass
        return result


def run_revenue_pipeline(db, sector=DENTIST_VERTICAL, location=None, hunt_count=8, audit_limit=5, send_limit=DAILY_EMAIL_LIMIT):
    """Fixed business pipeline: hunt -> audit -> score -> write outreach -> send -> metrics."""
    summary = {"sector": sector, "hunted": 0, "audited": 0, "qualified": 0, "queued": 0, "sent": 0}

    hunt = scrape_google_maps(db, {"sector": sector, "location": location or _rotation_city(), "count": hunt_count})
    summary["hunted"] = hunt.get("prospects_found", 0)
    if summary["hunted"]:
        _metric_increment(db, "leads_found", summary["hunted"])

    auditor = WebsiteAuditAgent()
    scorer = LeadScoring()
    generator = OutreachGenerator()
    sender = CampaignSender()

    try:
        prospects = db.table("prospects").select("*").eq("sector", sector).or_("status.is.null,status.in.(new,discovered)").limit(audit_limit).execute()
    except Exception:
        prospects = db.table("prospects").select("*").eq("sector", sector).not_.in_("status", ["contacted","followup_1","qualified","discarded","dead","warm_1","warm_2","warm_closed","converted"]).limit(audit_limit).execute()

    for prospect in prospects.data or []:
        website = prospect.get("website")
        if not website:
            continue
        audit = auditor.audit(website, prospect.get("company_name", ""))
        scoring = scorer.score(prospect, audit)
        summary["audited"] += 1
        status = "qualified" if scoring["qualified"] else "discarded"
        try:
            db.table("prospects").update({
                "status": status,
                "score": scoring["score"],
                "agent_notes": json.dumps({"audit": audit, "score_reason": scoring["reason"]}, ensure_ascii=False)[:900],
            }).eq("id", prospect.get("id")).execute()
        except Exception as e:
            print(f"prospect audit update skipped: {e}")
        if not scoring["qualified"]:
            continue
        summary["qualified"] += 1
        message = generator.generate(prospect, audit, scoring)
        queued = sender.queue(db, prospect, audit, scoring, message)
        if queued.get("queued"):
            summary["queued"] += 1

    summary["sent"] = sender.send_queued(db, limit=send_limit)
    return summary


SETTORE_CONTESTO = {
    "fotovoltaico": "installazione e manutenzione di pannelli solari fotovoltaici",
    "climatizzazione": "installazione e assistenza di impianti di climatizzazione e condizionatori",
    "idraulici": "servizi idraulici, riparazioni tubature, impianti idrici",
    "ristrutturazioni": "ristrutturazioni edili, lavori di muratura e costruzioni",
    "infissi": "installazione e sostituzione di infissi, finestre e porte",
}


def process_inbound_email(db, to_address, from_email, from_name, subject, body_text):
    """
    Called by /webhook/email-inbound when a client's customer sends an email.
    Looks up the client config by forwarding address, generates an AI reply, sends it,
    and notifies the business owner.
    """
    import anthropic

    # Look up client config by forwarding address
    try:
        result = db.table("client_configs").select("*").eq("forwarding_address", to_address).execute()
        if not result.data:
            print(f"process_inbound_email: no client found for {to_address}")
            return False
        config = result.data[0]
    except Exception as e:
        print(f"process_inbound_email lookup error: {e}")
        return False

    nome_azienda = config.get("nome_azienda", "l'azienda")
    settore = config.get("settore", "generale")
    email_titolare = config.get("email_titolare", "")
    config_id = config.get("id")
    contesto_settore = SETTORE_CONTESTO.get(settore, settore)

    # Generate AI response via Claude Haiku
    client_ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = f"""Sei l'assistente virtuale di "{nome_azienda}", un'azienda specializzata in {contesto_settore}.

Un cliente potenziale ha inviato questa richiesta via email:
Mittente: {from_name or from_email}
Oggetto: {subject}
Messaggio:
{body_text[:1500]}

Scrivi una risposta professionale, cordiale e utile in italiano che:
1. Ringrazia il cliente per la richiesta
2. Conferma la ricezione e che qualcuno li contatterà presto
3. Chiede eventuali informazioni aggiuntive necessarie (se la richiesta è vaga)
4. Firma come assistente di {nome_azienda}

La risposta deve essere concisa (3-5 frasi), professionale e rassicurante."""

    try:
        msg = client_ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        ai_response = msg.content[0].text.strip()
    except Exception as e:
        print(f"Claude API error: {e}")
        ai_response = (
            f"Gentile {from_name or 'Cliente'},\n\n"
            f"Grazie per aver contattato {nome_azienda}. Abbiamo ricevuto la sua richiesta "
            f"e la contatteremo al piu' presto per fornirle tutta l'assistenza necessaria.\n\n"
            f"Cordiali saluti,\nIl team di {nome_azienda}"
        )

    # Send reply to customer
    reply_subject = f"Re: {subject}" if not subject.startswith("Re:") else subject
    _send_plain_email(from_email, reply_subject, ai_response, sender_name=nome_azienda)

    # Notify business owner
    if email_titolare:
        notify_subject = f"FlowOps: nuova richiesta cliente ricevuta"
        notify_body = (
            f"Ciao,\n\n"
            f"FlowOps ha ricevuto e risposto automaticamente a una richiesta cliente:\n\n"
            f"Da: {from_name or from_email} <{from_email}>\n"
            f"Oggetto: {subject}\n\n"
            f"---\n{body_text[:500]}\n---\n\n"
            f"Risposta inviata automaticamente:\n{ai_response}\n\n"
            f"Accedi alla dashboard per gestire la conversazione."
        )
        _send_plain_email(email_titolare, notify_subject, notify_body, sender_name="FlowOps")

    # Save to requests table
    try:
        db.table("requests").insert({
            "client_config_id": config_id,
            "from_email": from_email,
            "from_name": from_name or "",
            "subject": subject,
            "body": body_text[:2000],
            "ai_response": ai_response,
            "status": "replied",
            "replied_at": datetime.now().isoformat(),
        }).execute()
    except Exception as e:
        print(f"requests insert error: {e}")

    # Telegram notification to owner
    send_telegram(
        f"FlowOps nuova richiesta!\n"
        f"Azienda: {nome_azienda} ({settore})\n"
        f"Da: {from_name or from_email}\n"
        f"Oggetto: {subject[:80]}\n"
        f"Risposta AI inviata automaticamente."
    )

    return True


