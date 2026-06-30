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

def _send_plain_email(to_email, subject, body, sender_name=None):
    """Send plain-text email with proper deliverability headers."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    EMAIL = os.getenv("EMAIL_ADDRESS")
    EMAIL_PASS = os.getenv("EMAIL_PASSWORD")
    if not EMAIL or not EMAIL_PASS or not to_email:
        return False
    try:
        display_name = sender_name or "GetAutomatik"
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{display_name} <{EMAIL}>"
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

def _scrape_email_from_website(website, _apify_token=None):
    """Extract email from business website using HTTP requests + regex."""
    import requests as r
    import re
    if not website:
        return None
    headers = {"User-Agent": "Mozilla/5.0 (compatible; GetAutomatik/1.0)"}
    pages_to_try = [website.rstrip("/"), website.rstrip("/") + "/contatti",
                    website.rstrip("/") + "/contact", website.rstrip("/") + "/chi-siamo"]
    email_re = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
    for url in pages_to_try:
        try:
            resp = r.get(url, headers=headers, timeout=8, allow_redirects=True)
            if resp.status_code != 200:
                continue
            text = resp.text
            # Decode obfuscated emails (e.g. "info [at] domain.it")
            text = re.sub(r"\s*\[at\]\s*", "@", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*\(at\)\s*", "@", text, flags=re.IGNORECASE)
            for email in email_re.findall(text):
                if _is_real_email(email):
                    return email.lower()
        except Exception:
            continue
    return None

def scrape_google_maps(db, params):
    import requests as r
    import os, time
    sector = params.get("sector", "idraulici")
    location = params.get("location") or _rotation_city()
    count = min(params.get("count", 10), 20)
    api_key = os.getenv("GOOGLE_PLACES_API_KEY", "")

    raw_results = []

    if api_key:
        try:
            # Step 1: Text Search
            query = f"{sector} {location} Italia"
            search_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
            resp = r.get(search_url, params={"query": query, "key": api_key, "language": "it"}, timeout=15)
            if resp.status_code == 200:
                places = resp.json().get("results", [])[:count]
                for place in places:
                    place_id = place.get("place_id", "")
                    name = place.get("name", "")
                    if not name or not place_id:
                        continue
                    # Step 2: Place Details to get website + phone
                    det_resp = r.get(
                        "https://maps.googleapis.com/maps/api/place/details/json",
                        params={"place_id": place_id, "fields": "name,website,formatted_phone_number", "key": api_key},
                        timeout=10
                    )
                    website = ""
                    phone = ""
                    if det_resp.status_code == 200:
                        det = det_resp.json().get("result", {})
                        website = det.get("website", "")
                        phone = det.get("formatted_phone_number", "")
                    raw_results.append({
                        "company_name": name,
                        "contact_email": "",
                        "contact_phone": phone,
                        "website": website,
                        "sector": sector,
                        "source": "google_places",
                        "score": 8
                    })
                    time.sleep(0.1)
        except Exception as e:
            print(f"Google Places error: {e}")
    else:
        print("GOOGLE_PLACES_API_KEY mancante")

    # Per ogni risultato con sito, estrai email
    prospects = []
    for p in raw_results:
        if p["website"]:
            email = _scrape_email_from_website(p["website"], None)
            if email:
                p["contact_email"] = email
                prospects.append(p)
        # senza email saltiamo

    _track_cost(db, 0.002 * len(raw_results), f"google_places {sector} {location}")

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

    if added:
        send_telegram(f"Trovati {added} prospect oggi — {sector} ({location})")

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
    """Classify reply with Claude, auto-respond, push warm prospects toward landing."""
    import anthropic

    company = prospect.get("company_name", "")
    email_to = prospect.get("contact_email", "")
    sector = prospect.get("sector", "")
    prospect_id = prospect.get("id", "")
    LANDING = os.getenv("FLOWOPS_LANDING", "https://getautomatik.com/landing")
    landing_url = f"{LANDING}?ref={prospect_id}" if prospect_id else LANDING

    claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))

    clf = claude_client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=10,
        messages=[{"role": "user", "content":
            f"Classifica questa risposta email di un'azienda italiana ({company}, settore {sector}) "
            f"a una proposta GetAutomatik (rispondente automatico AI):\n\"{body_text[:400]}\"\n\n"
            f"Rispondi SOLO con: INTERESTED / NOT_INTERESTED / QUESTION / OUT_OF_OFFICE"
        }]
    ).content[0].text.strip().upper()

    if "INTERESTED" in clf:
        body = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=300,
            messages=[{"role": "user", "content":
                f"Scrivi una email di chiusura vendita (100 parole max) per {company} (settore {sector}) "
                f"che ha mostrato interesse a GetAutomatik, il sistema AI che risponde automaticamente ai clienti.\n"
                f"1. Apertura personalizzata per {sector}\n"
                f"2. Beneficio: nessuna richiesta persa, risposta entro 2 minuti\n"
                f"3. CTA: prova gratis 7 giorni -> {landing_url}\n"
                f"4. P.S.: attivo in 15 minuti, setup guidato\n"
                f"Tono: caldo, diretto, zero pressione. Firma: Team GetAutomatik"
            }]
        ).content[0].text

        _send_plain_email(email_to, f"Come funziona GetAutomatik per {company}", body, sender_name="GetAutomatik")

        warm_date = (date.today() + timedelta(days=2)).isoformat()
        db.table("prospects").update({
            "status": "warm_1",
            "follow_up_at": warm_date,
            "replied_at": date.today().isoformat(),
            "agent_notes": f"INTERESTED: {body_text[:120]}"
        }).eq("id", prospect["id"]).execute()
        send_telegram(f"RISPOSTA da {company} ({sector})! Controlla email\nClassificato: INTERESSATO\nEmail: {email_to}")

    elif "QUESTION" in clf:
        body = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=250,
            messages=[{"role": "user", "content":
                f"Un artigiano italiano ({company}, {sector}) ha fatto questa domanda su GetAutomatik:\n\"{body_text[:300]}\"\n"
                f"Rispondi in modo convincente e diretto (max 80 parole). "
                f"GetAutomatik: AI che risponde ai clienti automaticamente, 197 EUR/mese, 7gg trial.\n"
                f"Chiudi con CTA: prova gratis -> {landing_url}\n"
                f"Firma: Team GetAutomatik"
            }]
        ).content[0].text
        _send_plain_email(email_to, f"Re: GetAutomatik per {company}", body, sender_name="GetAutomatik")
        db.table("prospects").update({
            "status": "warm_1",
            "follow_up_at": (date.today() + timedelta(days=3)).isoformat(),
            "replied_at": date.today().isoformat(),
            "agent_notes": f"QUESTION: {body_text[:120]}"
        }).eq("id", prospect["id"]).execute()
        send_telegram(f"RISPOSTA da {company} ({sector})! Controlla email\nDomanda ricevuta — risposta + link inviati")

    elif "NOT_INTERESTED" in clf:
        db.table("prospects").update({
            "status": "dead",
            "replied_at": date.today().isoformat(),
            "agent_notes": f"NOT_INTERESTED: {body_text[:120]}"
        }).eq("id", prospect["id"]).execute()
        send_telegram(f"{company} non interessato -- archiviato")

    elif "OUT_OF_OFFICE" in clf:
        db.table("prospects").update({
            "status": "contacted",
            "follow_up_at": (date.today() + timedelta(days=5)).isoformat(),
            "agent_notes": "OUT_OF_OFFICE — ricontatto +5gg"
        }).eq("id", prospect["id"]).execute()

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
    claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
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
            f"Scrivi email fredda in italiano (max 110 parole) per {company} (settore {sector}). "
            f"Proponi GetAutomatik: sistema AI che risponde automaticamente alle email dei loro clienti. "
            f"Oggetto: quante richieste perdi ogni settimana? "
            f"Tono: diretto, specifico per artigiani. CTA: prova gratis su getautomatik.com/landing"
            f"Firma: Team GetAutomatik. No template generico."
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

    sent = _send_plain_email(email_to, f"{company} — quante richieste perdi ogni settimana?", email_body, sender_name="GetAutomatik")

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
    LANDING = os.getenv("FLOWOPS_LANDING", "https://getautomatik.com/landing")

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
    claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

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
                    f"su GetAutomatik. CTA: risposta si/no. Firma: Team GetAutomatik"
                )
                new_status = "followup_1"
                next_date = (date.today() + timedelta(days=4)).isoformat()
                subject = f"Re: {company}"

            elif status == "followup_1":
                prompt = (
                    f"Email finale (max 55 parole) per {company} ({sector}). "
                    f"Ultima email. Offri trial gratuito 7 giorni: {LANDING} "
                    f"Firma: Team GetAutomatik"
                )
                new_status = "dead"
                next_date = None
                subject = f"Ultima email — {company}"

            # --- Warm closing sequence (prospect ha già risposto con interesse) ---
            elif status == "warm_1":
                if _check_converted(db, email_to):
                    db.table("prospects").update({"status": "converted"}).eq("id", p["id"]).execute()
                    continue
                prompt = (
                    f"Follow-up caldo (max 70 parole) per {company} ({sector}) che aveva mostrato interesse "
                    f"ma non ha ancora attivato GetAutomatik. Ricorda il valore, zero pressione. "
                    f"Link diretto: {LANDING} Firma: Team GetAutomatik"
                )
                new_status = "warm_2"
                next_date = (date.today() + timedelta(days=3)).isoformat()
                subject = f"{company} — il trial GetAutomatik e' ancora disponibile"

            elif status == "warm_2":
                if _check_converted(db, email_to):
                    db.table("prospects").update({"status": "converted"}).eq("id", p["id"]).execute()
                    continue
                prompt = (
                    f"Email di chiusura finale (max 60 parole) per {company} ({sector}). "
                    f"Offri 14 giorni di trial gratuito invece di 7 — offerta valida solo questa settimana. "
                    f"Link diretto: {LANDING} "
                    f"Firma: Team GetAutomatik"
                )
                new_status = "warm_closed"
                next_date = None
                subject = f"14 giorni gratis per {company} — offerta a tempo (GetAutomatik)"

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

        claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
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
    claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
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
    allowed = {"prospects_found", "emails_sent", "replies_received", "new_clients", "mrr_total", "requests_handled", "value_recovered"}
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
                "prospects_found": 0,
                "emails_sent": 0,
                "replies_received": 0,
                "new_clients": 0,
                "mrr_total": 0,
                "requests_handled": 0,
                "value_recovered": 0,
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
        self.api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")

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
                    "Analizza questo sito di un'azienda artigianale italiana. "
                    "Usa solo il contenuto fornito. Trova problemi che fanno perdere richieste clienti "
                    "e opportunita per recuperare richieste non risposte. "
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
        if (prospect.get("sector") or "").lower() in ("fotovoltaico", "climatizzazione", "idraulici", "ristrutturazioni", "infissi", "dentisti", "dentista", "studio dentistico"):
            score += 15
        if audit.get("estimated_lost_leads", 0) >= 4:
            score += 15
        if audit.get("score", 0) < 65:
            score += 10
        if prospect.get("contact_phone"):
            score += 5
        reason = "email business + sito + audit artigiani" if score >= 65 else "lead debole o audit poco urgente"
        return {"score": min(100, score), "qualified": score >= 65, "reason": reason}


class OutreachGenerator:
    """Creates audit-led outreach copy for dentists."""

    def __init__(self):
        self.api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")

    def generate(self, prospect, audit, score, email_number=1):
        company = prospect.get("company_name") or audit.get("company_name") or "Azienda"
        sector = prospect.get("sector", "artigiano")
        prospect_id = prospect.get("id", "")
        first_problem = (audit.get("problems") or ["alcune richieste clienti possono perdersi"])[0]
        lost = audit.get("estimated_lost_leads", 3)
        LANDING = os.getenv("FLOWOPS_LANDING", "https://getautomatik.com/landing")
        landing_url = f"{LANDING}?ref={prospect_id}" if prospect_id else LANDING

        subjects = {
            1: f"{company} — quante richieste perdi ogni settimana?",
            2: f"Re: {company}",
            3: f"Ultima email — {company}",
        }
        subject = subjects.get(email_number, subjects[1])

        if self.api_key:
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=self.api_key)
                if email_number == 1:
                    prompt = (
                        f"Scrivi email cold B2B in italiano (max 110 parole) per {company} (settore {sector}).\n"
                        f"Proponi GetAutomatik: sistema AI che risponde automaticamente alle email dei loro clienti.\n"
                        f"Problema rilevato dal sito: {first_problem}\n"
                        f"Stima richieste perse/mese: {lost}\n"
                        f"CTA: scopri come funziona -> {landing_url}\n"
                        f"Tono: diretto, specifico, umano. Firma: Team GetAutomatik. NO template generico."
                    )
                elif email_number == 2:
                    prompt = (
                        f"Scrivi follow-up brevissimo (max 60 parole) per {company} ({sector}).\n"
                        f"E' il secondo contatto — sii ancora piu' breve e diretto.\n"
                        f"Menziona un caso studio specifico per {sector}: un artigiano che non perdeva piu' richieste.\n"
                        f"CTA: risposta rapida o link -> {landing_url}\n"
                        f"Firma: Team GetAutomatik"
                    )
                else:
                    prompt = (
                        f"Scrivi email finale (max 55 parole) per {company} ({sector}).\n"
                        f"E' l'ultima email — sii diretto e usa la scarsita'.\n"
                        f"CTA: trial gratis 7 giorni, link -> {landing_url}\n"
                        f"Firma: Team GetAutomatik"
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
                f"Buongiorno,\n\nHo analizzato il sito di {company}: {first_problem}.\n\n"
                f"GetAutomatik risponde automaticamente alle email dei vostri clienti entro 2 minuti, "
                f"anche fuori orario.\n\nProva gratis 7 giorni: {landing_url}\n\nTeam GetAutomatik"
            )
        return {
            "subject": subject,
            "body": body,
            "cta": "Prova GetAutomatik gratis",
            "email_number": email_number,
        }


class CampaignSender:
    """Sends queued outreach with existing SMTP and logs to Supabase."""

    def queue(self, db, prospect, audit, scoring, message):
        email_number = message.get("email_number", 1)
        payload = {
            "prospect_id": prospect.get("id"),
            "company_name": prospect.get("company_name") or audit.get("company_name"),
            "contact_email": prospect.get("contact_email"),
            "sector": prospect.get("sector") or DENTIST_VERTICAL,
            "status": "queued",
            "score": scoring.get("score"),
            "subject": message.get("subject"),
            "body": message.get("body"),
            "email_number": email_number,
            "audit": json.dumps(audit, ensure_ascii=False),
            "created_at": datetime_now_iso(),
        }
        try:
            existing = db.table("outreach").select("id,status").eq("contact_email", payload["contact_email"]).eq("email_number", email_number).execute()
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
            email_number = row.get("email_number", 1)
            if not _is_real_email(email_to):
                db.table("outreach").update({"status": "skipped", "error": "invalid_email"}).eq("id", row.get("id")).execute()
                continue
            if not _check_and_increment_daily_emails(db):
                break
            ok = _send_plain_email(email_to, row.get("subject", "GetAutomatik"), row.get("body", ""), sender_name="GetAutomatik")
            status = "sent" if ok else "failed"
            update = {"status": status, "sent_at": datetime_now_iso()}
            if not ok:
                update["error"] = "smtp_failed"
            try:
                db.table("outreach").update(update).eq("id", row.get("id")).execute()
                # Track in outreach_emails table
                if row.get("prospect_id"):
                    try:
                        db.table("outreach_emails").insert({
                            "prospect_id": row.get("prospect_id"),
                            "email_number": email_number,
                            "subject": row.get("subject"),
                            "body": (row.get("body") or "")[:1000],
                            "status": status,
                            "sent_at": datetime_now_iso(),
                        }).execute()
                    except Exception:
                        pass
                    next_days = {1: 3, 2: 4, 3: None}
                    nxt = next_days.get(email_number)
                    if ok:
                        upd = {"status": "contacted", "contacted_at": datetime_now_iso()}
                        if nxt:
                            upd["follow_up_at"] = (date.today() + timedelta(days=nxt)).isoformat()
                        db.table("prospects").update(upd).eq("id", row.get("prospect_id")).execute()
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
        result = {"prospects_found": 0, "emails_sent": 0, "replies_received": 0, "new_clients": 0, "mrr_total": 0, "requests_handled": 0}
        try:
            metrics = db.table("daily_metrics").select("*").eq("date", today).execute()
            if metrics.data:
                row = metrics.data[0]
                result.update({
                    "prospects_found": _safe_int(row.get("prospects_found"), 0),
                    "emails_sent": _safe_int(row.get("emails_sent"), 0),
                    "replies_received": _safe_int(row.get("replies_received"), 0),
                    "new_clients": _safe_int(row.get("new_clients"), 0),
                    "mrr_total": _safe_int(row.get("mrr_total"), 0),
                    "requests_handled": _safe_int(row.get("requests_handled"), 0),
                })
        except Exception as e:
            print(f"daily_metrics read fallback: {e}")
        try:
            clients = db.table("clients").select("mrr").eq("status", "active").execute()
            result["new_clients"] = len(clients.data or [])
            result["mrr_total"] = sum(_safe_int(c.get("mrr"), 0) for c in (clients.data or []))
        except Exception:
            pass
        return result


def run_revenue_pipeline(db, sector=DENTIST_VERTICAL, location=None, hunt_count=8, audit_limit=5, send_limit=DAILY_EMAIL_LIMIT):
    """Fixed business pipeline: hunt -> audit -> score -> write outreach -> send -> metrics."""
    summary = {"sector": sector, "hunted": 0, "audited": 0, "qualified": 0, "queued": 0, "sent": 0}

    hunt = scrape_google_maps(db, {"sector": sector, "location": location or _rotation_city(), "count": hunt_count})
    summary["hunted"] = hunt.get("prospects_found", 0)
    if summary["hunted"]:
        _metric_increment(db, "prospects_found", summary["hunted"])

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

# Fasce di valore di default per settore usate come fallback di classify_request
SETTORE_VALUE_RANGE = {
    "fotovoltaico":    (4000, 12000),
    "climatizzazione": (800,  3000),
    "idraulici":       (200,  1500),
    "ristrutturazioni":(5000, 30000),
    "infissi":         (1500, 6000),
}


def classify_request(client_ai, body_text, settore, nome_azienda):
    """
    Classifica la richiesta del cliente e stima il valore economico.
    Returns dict: {lead_type, estimated_value}
    """
    value_range = SETTORE_VALUE_RANGE.get(settore, (500, 5000))
    prompt = (
        f"Sei un analista business per '{nome_azienda}', settore {settore}.\n\n"
        f"Analizza questa richiesta cliente e rispondi SOLO con JSON valido, nessun altro testo:\n\n"
        f"Richiesta:\n{body_text[:800]}\n\n"
        f"Rispondi con questo JSON esatto:\n"
        f'{{"lead_type": "preventivo|info|urgenza|altro", '
        f'"estimated_value": <numero intero in euro, fascia {value_range[0]}-{value_range[1]}>, '
        f'"reason": "<breve motivazione>"}}'
    )
    try:
        msg = client_ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        # Extract JSON from response (handle markdown code blocks)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            lead_type = data.get("lead_type", "altro")
            if lead_type not in ("preventivo", "info", "urgenza", "altro"):
                lead_type = "altro"
            estimated_value = int(data.get("estimated_value", value_range[0]))
            return {"lead_type": lead_type, "estimated_value": estimated_value}
    except Exception as e:
        print(f"classify_request error: {e}")

    # Fallback: keyword-based classification
    text_lower = body_text.lower()
    if any(w in text_lower for w in ("urgente", "urgenza", "guasto", "perdita", "rottura", "emergenza")):
        lead_type = "urgenza"
        estimated_value = value_range[0]
    elif any(w in text_lower for w in ("preventivo", "quanto costa", "prezzo", "offerta", "costo")):
        lead_type = "preventivo"
        estimated_value = (value_range[0] + value_range[1]) // 2
    elif any(w in text_lower for w in ("informazioni", "info", "come funziona", "vorrei sapere")):
        lead_type = "info"
        estimated_value = value_range[0]
    else:
        lead_type = "altro"
        estimated_value = value_range[0]
    return {"lead_type": lead_type, "estimated_value": estimated_value}


def generate_reply(client_ai, body_text, nome_azienda, settore, from_name):
    """
    Genera risposta professionale AI per il cliente.
    Returns stringa testo risposta.
    """
    contesto_settore = SETTORE_CONTESTO.get(settore, settore)
    prompt = (
        f'Sei l\'assistente virtuale di "{nome_azienda}", specializzata in {contesto_settore}.\n\n'
        f"Un cliente ha inviato questa richiesta:\nMittente: {from_name}\n\n{body_text[:1500]}\n\n"
        f"Scrivi una risposta in italiano (3-5 frasi) che:\n"
        f"1. Ringrazia il cliente\n"
        f"2. Conferma la ricezione e che qualcuno li contatterà presto\n"
        f"3. Se la richiesta e' vaga, chiede dettagli specifici\n"
        f"4. Firma come assistente di {nome_azienda}\n\n"
        f"Solo il testo della risposta, nessun altro commento."
    )
    try:
        msg = client_ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"generate_reply error: {e}")
        return (
            f"Gentile {from_name or 'Cliente'},\n\n"
            f"Grazie per aver contattato {nome_azienda}. Abbiamo ricevuto la sua richiesta "
            f"e la contatteremo al piu' presto.\n\n"
            f"Cordiali saluti,\nIl team di {nome_azienda}"
        )


def notify_owner(email_titolare, from_email, from_name, subject, body_text, ai_response,
                 lead_type=None, estimated_value=None, nome_azienda="GetAutomatik"):
    """
    Invia email di notifica al titolare dell'azienda con il dettaglio della richiesta e la risposta AI.
    """
    if not email_titolare:
        return False
    value_line = f"Valore stimato: EUR {estimated_value:,}" if estimated_value else ""
    type_line = f"Tipo richiesta: {lead_type}" if lead_type else ""
    body = (
        f"GetAutomatik ha ricevuto e risposto automaticamente a una richiesta cliente.\n\n"
        f"Da: {from_name or from_email} <{from_email}>\n"
        f"Oggetto: {subject}\n"
        f"{type_line}\n{value_line}\n\n"
        f"--- Richiesta ---\n{body_text[:600]}\n\n"
        f"--- Risposta inviata ---\n{ai_response}\n\n"
        f"Accedi alla dashboard per gestire la conversazione."
    )
    return _send_plain_email(
        email_titolare,
        f"GetAutomatik: nuova richiesta {lead_type or ''} da {from_name or from_email}",
        body,
        sender_name="GetAutomatik"
    )


def save_request(db, client_config_id, data):
    """
    Salva la richiesta nella tabella requests.
    data: dict con chiavi from_email, from_name, subject, body, ai_response,
          lead_type, estimated_value, source, replied_at
    """
    try:
        db.table("requests").insert({
            "client_config_id": client_config_id,
            "from_email": data.get("from_email", ""),
            "from_name": data.get("from_name", ""),
            "subject": data.get("subject", ""),
            "body": (data.get("body", ""))[:2000],
            "ai_response": data.get("ai_response", ""),
            "lead_type": data.get("lead_type"),
            "estimated_value": data.get("estimated_value"),
            "source": data.get("source", "email_forwarding"),
            "followup_count": 0,
            "converted": False,
            "status": "replied",
            "replied_at": data.get("replied_at", datetime.now().isoformat()),
        }).execute()
        return True
    except Exception as e:
        print(f"save_request error: {e}")
        return False


def process_inbound_email(db, to_address, from_email, from_name, subject, body_text):
    """
    Orchestrates the full pipeline when a customer email arrives via forwarding.
    1. Lookup client config
    2. Classify request (lead_type, estimated_value)
    3. Generate AI reply
    4. Send reply to customer
    5. Notify owner
    6. Save to DB
    7. Telegram alert
    """
    import anthropic

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

    client_ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    classification = classify_request(client_ai, body_text, settore, nome_azienda)
    lead_type = classification["lead_type"]
    estimated_value = classification["estimated_value"]

    ai_response = generate_reply(client_ai, body_text, nome_azienda, settore, from_name or from_email)

    reply_subject = f"Re: {subject}" if not subject.lower().startswith("re:") else subject
    _send_plain_email(from_email, reply_subject, ai_response, sender_name=nome_azienda)

    notify_owner(
        email_titolare, from_email, from_name, subject, body_text, ai_response,
        lead_type=lead_type, estimated_value=estimated_value, nome_azienda=nome_azienda
    )

    save_request(db, config_id, {
        "from_email": from_email,
        "from_name": from_name or "",
        "subject": subject,
        "body": body_text,
        "ai_response": ai_response,
        "lead_type": lead_type,
        "estimated_value": estimated_value,
        "source": "email_forwarding",
        "replied_at": datetime.now().isoformat(),
    })

    send_telegram(
        f"GetAutomatik nuova richiesta [{lead_type}]!\n"
        f"Azienda: {nome_azienda} ({settore})\n"
        f"Da: {from_name or from_email}\n"
        f"Oggetto: {subject[:80]}\n"
        f"Valore stimato: EUR {estimated_value:,}\n"
        f"Risposta AI inviata."
    )

    try:
        _metric_increment(db, "requests_handled", 1)
        if estimated_value:
            _metric_increment(db, "value_recovered", estimated_value)
    except Exception:
        pass

    return True


def send_request_followups(db):
    """
    Controlla le richieste senza risposta del cliente dopo 48h e invia follow-up automatici.
    Max 2 follow-up per richiesta. Incrementa followup_count.
    """
    import anthropic
    from datetime import timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    try:
        rows = (
            db.table("requests")
            .select("*, client_configs(nome_azienda, settore, email_titolare)")
            .eq("status", "replied")
            .eq("converted", False)
            .lt("replied_at", cutoff)
            .lt("followup_count", 2)
            .execute()
        )
    except Exception as e:
        print(f"send_request_followups query error: {e}")
        return 0

    if not rows.data:
        return 0

    client_ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    sent = 0
    for row in rows.data:
        from_email = row.get("from_email", "")
        from_name = row.get("from_name", "")
        subject = row.get("subject", "")
        original_reply = row.get("ai_response", "")
        req_id = row.get("id")
        followup_count = row.get("followup_count", 0)

        config = row.get("client_configs") or {}
        nome_azienda = config.get("nome_azienda", "la nostra azienda")
        settore = config.get("settore", "generale")

        if not from_email:
            continue

        prompt = (
            f"Sei l'assistente di '{nome_azienda}' (settore {settore}).\n"
            f"Hai risposto a una richiesta cliente {48 * (followup_count + 1)}h fa ma non hai ricevuto risposta.\n"
            f"Scrivi un brevissimo follow-up (2-3 frasi) in italiano, cordiale e non invasivo, "
            f"per assicurarti che abbiano ricevuto la risposta e se hanno bisogno di aiuto.\n"
            f"Firma come assistente di {nome_azienda}. Solo il testo, nessun commento."
        )
        try:
            msg = client_ai.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}]
            )
            followup_text = msg.content[0].text.strip()
        except Exception as e:
            print(f"followup generation error: {e}")
            followup_text = (
                f"Buongiorno {from_name or ''},\n\n"
                f"Volevamo assicurarci che avesse ricevuto la nostra risposta riguardo la sua richiesta.\n"
                f"Siamo a disposizione per qualsiasi domanda.\n\n"
                f"Cordiali saluti,\nIl team di {nome_azienda}"
            )

        followup_subject = f"Re: {subject}" if not subject.lower().startswith("re:") else subject
        ok = _send_plain_email(from_email, followup_subject, followup_text, sender_name=nome_azienda)
        if ok:
            try:
                db.table("requests").update({
                    "followup_count": followup_count + 1,
                    "status": "followup_sent",
                }).eq("id", req_id).execute()
            except Exception as e:
                print(f"followup update error: {e}")
            sent += 1

    return sent


def metrics_report(db):
    """
    Analizza performance 72h: settore migliore, citta' migliore, email che converte di piu'.
    Aggiorna market scores. Invia report Telegram.
    """
    import anthropic

    try:
        all_p = db.table("prospects").select("sector,status,company_name,agent_notes,contacted_at,replied_at").execute()
        rows = all_p.data or []
    except Exception as e:
        print(f"metrics_report query error: {e}")
        return

    # Per-sector stats
    sectors = {}
    for p in rows:
        s = p.get("sector", "altro")
        if s not in sectors:
            sectors[s] = {"total": 0, "contacted": 0, "replied": 0, "converted": 0}
        sectors[s]["total"] += 1
        if p.get("status") in ("contacted", "followup_1", "replied", "warm_1", "warm_2", "warm_closed", "converted"):
            sectors[s]["contacted"] += 1
        if p.get("status") in ("replied", "warm_1", "warm_2", "warm_closed", "converted"):
            sectors[s]["replied"] += 1
        if p.get("status") == "converted":
            sectors[s]["converted"] += 1

    best_sector = max(sectors, key=lambda s: sectors[s]["replied"] / max(sectors[s]["contacted"], 1)) if sectors else "n/d"

    # Active clients
    try:
        clients = db.table("clients").select("mrr").eq("status", "active").execute()
        n_clients = len(clients.data or [])
        mrr = sum(c.get("mrr", 0) or 0 for c in (clients.data or []))
    except Exception:
        n_clients, mrr = 0, 0

    # Requests this month
    from_date = datetime.now().replace(day=1, hour=0, minute=0, second=0).isoformat()
    try:
        reqs = db.table("requests").select("estimated_value,converted").gte("received_at", from_date).execute()
        req_data = reqs.data or []
        req_value = sum(r.get("estimated_value") or 0 for r in req_data)
        req_converted = sum(1 for r in req_data if r.get("converted"))
    except Exception:
        req_value, req_converted = 0, 0

    total_contacted = sum(s["contacted"] for s in sectors.values())
    total_replied = sum(s["replied"] for s in sectors.values())
    reply_rate = round(total_replied / total_contacted * 100, 1) if total_contacted else 0

    # Update market scores in DB
    for sector_name, stats in sectors.items():
        if stats["contacted"] >= 5:
            rr = stats["replied"] / stats["contacted"]
            score = min(100, int(rr * 1000))  # 10% reply rate = score 100
            try:
                existing = db.table("markets").select("id").eq("sector", sector_name).execute()
                if existing.data:
                    db.table("markets").update({"score": score, "conversion_rate": round(rr * 100, 1)}).eq("sector", sector_name).execute()
            except Exception:
                pass

    sector_lines = "\n".join(
        f"  {s}: {d['contacted']} contattati, {d['replied']} risposte ({round(d['replied']/max(d['contacted'],1)*100,1)}%)"
        for s, d in sorted(sectors.items(), key=lambda x: x[1]["replied"], reverse=True)
    ) or "  Nessun dato"

    send_telegram(
        f"Report 72h GetAutomatik:\n"
        f"Email inviate: {total_contacted} | Risposte: {total_replied} | Reply rate: {reply_rate}%\n"
        f"Clienti attivi: {n_clients} | MRR: EUR {mrr}\n"
        f"Richieste mese: EUR {req_value:,} | Convertite: {req_converted}\n"
        f"Settore migliore: {best_sector}\n\n"
        f"Per settore:\n{sector_lines}"
    )
    return {"sectors": sectors, "best_sector": best_sector, "reply_rate": reply_rate}


def ceo_pivot(db):
    """
    Analizza reply rate per settore ogni 6h.
    - reply rate < 2% dopo 50 email: abbassa score mercato
    - reply rate > 5%: aumenta volume e score
    Invia Telegram con decisione.
    """
    try:
        all_p = db.table("prospects").select("sector,status").execute()
        rows = all_p.data or []
    except Exception as e:
        print(f"ceo_pivot query error: {e}")
        return

    sectors = {}
    for p in rows:
        s = p.get("sector", "altro")
        if s not in sectors:
            sectors[s] = {"contacted": 0, "replied": 0}
        if p.get("status") in ("contacted", "followup_1", "replied", "warm_1", "warm_2", "warm_closed", "converted", "dead"):
            sectors[s]["contacted"] += 1
        if p.get("status") in ("replied", "warm_1", "warm_2", "warm_closed", "converted"):
            sectors[s]["replied"] += 1

    decisions = []
    for sector_name, stats in sectors.items():
        contacted = stats["contacted"]
        replied = stats["replied"]
        if contacted < 10:
            continue
        rr = replied / contacted * 100
        try:
            mkt = db.table("markets").select("score").eq("sector", sector_name).execute()
            current_score = mkt.data[0]["score"] if mkt.data else 50
        except Exception:
            current_score = 50

        if rr < 2.0 and contacted >= 50:
            new_score = max(10, current_score - 15)
            action = f"Abbasso priorita' {sector_name} (reply rate {rr:.1f}% su {contacted} email)"
        elif rr > 5.0:
            new_score = min(100, current_score + 20)
            action = f"Aumento volume {sector_name} (reply rate {rr:.1f}% -- ottimo!)"
        else:
            continue

        try:
            db.table("markets").update({"score": new_score}).eq("sector", sector_name).execute()
            db.table("decisions").insert({
                "agent_name": "CEO",
                "thought_process": f"Reply rate {sector_name}: {rr:.1f}% su {contacted} email",
                "decision": "PIVOT_SETTORE",
                "action_taken": action,
                "result": f"Score: {current_score} -> {new_score}",
            }).execute()
        except Exception as e:
            print(f"ceo_pivot update error: {e}")

        decisions.append(f"Pivot: {action} (score {current_score}->{new_score})")

    if decisions:
        send_telegram("CEO Pivot:\n" + "\n".join(decisions))

    return decisions


# ═══════════════════════════════════════════════════════════════════
# CHAT WIDGET — qualificazione lead via chatbot sul sito del cliente
# ═══════════════════════════════════════════════════════════════════

def chat_qualify_lead(client_config, messages):
    """Continue chat conversation and qualify lead."""
    import anthropic, json

    settore = client_config.get("settore", "servizi")
    nome_azienda = client_config.get("nome_azienda", "l'azienda")
    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
    fallback = {"reply": "Ciao! Come posso aiutarti?", "qualified": False,
                "lead_name": None, "lead_phone": None, "lead_type": None}
    if not api_key:
        return fallback

    system = (
        f"Sei l'assistente AI di {nome_azienda}, settore {settore}.\n"
        f"Stai chattando con un visitatore del sito web.\n"
        f"Obiettivo: capire il tipo di intervento, raccogliere nome e numero di telefono, poi concludere.\n"
        f"REGOLE: rispondi in italiano, max 2 frasi, stile conversazionale.\n"
        f"Prima capisci cosa serve, poi chiedi nome e telefono.\n"
        f"Rispondi SOLO con JSON valido:\n"
        f'{"{"}"reply":"...","qualified":false,"lead_name":null,"lead_phone":null,"lead_type":null{"}"}\n'
        f"Quando hai nome E telefono: qualified=true e compila tutti i campi."
    )
    try:
        client_ai = anthropic.Anthropic(api_key=api_key)
        resp = client_ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system,
            messages=messages,
        )
        text = resp.content[0].text.strip()
        s = text.find("{")
        e = text.rfind("}") + 1
        if s >= 0 and e > s:
            return json.loads(text[s:e])
    except Exception as ex:
        print(f"chat_qualify_lead error: {ex}")
    return fallback


def notify_chat_lead(client_config, lead_name, lead_phone, lead_type):
    """Email owner when a chat lead is qualified."""
    owner_email = client_config.get("email_titolare")
    nome_azienda = client_config.get("nome_azienda", "")
    if not owner_email:
        return
    body = (
        f"Hai un nuovo lead dal chatbot del tuo sito!\n\n"
        f"Nome: {lead_name or 'Non fornito'}\n"
        f"Telefono: {lead_phone or 'Non fornito'}\n"
        f"Tipo lavoro: {lead_type or 'Non specificato'}\n\n"
        f"Ricontattalo il prima possibile."
    )
    _send_plain_email(owner_email, f"Nuovo lead dal sito: {lead_name or lead_phone}", body, sender_name="GetAutomatik")
    send_telegram(
        f"Lead chatbot!\n"
        f"Azienda: {nome_azienda}\n"
        f"Lead: {lead_name} — {lead_phone}\n"
        f"Lavoro: {lead_type}"
    )


# ═══════════════════════════════════════════════════════════════════
# TWILIO SMS — Missed Call Text-Back + conversazione SMS
# ═══════════════════════════════════════════════════════════════════

def send_twilio_sms(to_phone, body, from_phone=None):
    """Send SMS via Twilio."""
    try:
        from twilio.rest import Client as TwilioClient
        sid = os.getenv("TWILIO_ACCOUNT_SID")
        token = os.getenv("TWILIO_AUTH_TOKEN")
        from_phone = from_phone or os.getenv("TWILIO_DEFAULT_NUMBER")
        if not all([sid, token, from_phone]):
            print("Twilio env vars mancanti")
            return False
        TwilioClient(sid, token).messages.create(body=body, from_=from_phone, to=to_phone)
        return True
    except Exception as ex:
        print(f"send_twilio_sms error: {ex}")
        return False


def _generate_missed_call_sms(settore, nome_azienda):
    """Generate personalized missed-call SMS with Claude Haiku."""
    import anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
    default = f"Ciao! Sono {nome_azienda}. Ho visto la tua chiamata — dimmi come posso aiutarti e ti ricontatto subito!"
    if not api_key:
        return default
    try:
        client_ai = anthropic.Anthropic(api_key=api_key)
        resp = client_ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content":
                f"SMS (max 140 caratteri) per cliente che ha chiamato senza risposta. "
                f"Azienda: {nome_azienda}, settore: {settore}. "
                f"Tono caldo. Invita a rispondere. Solo testo SMS, niente altro."}]
        )
        return resp.content[0].text.strip().strip('"')
    except Exception:
        return default


def handle_missed_call(db, twilio_number, caller_phone):
    """Process missed call: find client, send first SMS, open conversation."""
    try:
        r = db.table("client_configs").select("*").eq("twilio_number", twilio_number).eq("active", True).execute()
        if not r.data:
            print(f"handle_missed_call: nessun cliente per {twilio_number}")
            return False
        client = r.data[0]
        existing = db.table("sms_conversations").select("id").eq("client_config_id", client["id"]).eq("caller_phone", caller_phone).execute()
        if existing.data:
            return True
        sms_body = _generate_missed_call_sms(client.get("settore", "servizi"), client.get("nome_azienda", "l'azienda"))
        if not send_twilio_sms(caller_phone, sms_body, twilio_number):
            return False
        db.table("sms_conversations").insert({
            "client_config_id": client["id"],
            "caller_phone": caller_phone,
            "messages": [{"role": "assistant", "content": sms_body}],
        }).execute()
        send_telegram(
            f"Chiamata persa recuperata!\n"
            f"Azienda: {client.get('nome_azienda')}\n"
            f"Chiamante: {caller_phone}"
        )
        return True
    except Exception as ex:
        print(f"handle_missed_call error: {ex}")
        return False


def handle_sms_inbound(db, twilio_number, from_phone, body):
    """Continue AI SMS conversation when caller replies. Returns reply text."""
    import anthropic, json
    try:
        r = db.table("client_configs").select("*").eq("twilio_number", twilio_number).eq("active", True).execute()
        if not r.data:
            return "Grazie per il messaggio!"
        client = r.data[0]
        client_id = client["id"]

        conv_r = db.table("sms_conversations").select("*").eq("client_config_id", client_id).eq("caller_phone", from_phone).execute()
        if not conv_r.data:
            db.table("sms_conversations").insert({"client_config_id": client_id, "caller_phone": from_phone, "messages": []}).execute()
            conv_r = db.table("sms_conversations").select("*").eq("client_config_id", client_id).eq("caller_phone", from_phone).execute()

        conv = conv_r.data[0]
        if conv.get("qualified"):
            return "Grazie! Il titolare ti contatterà presto."

        messages = conv.get("messages") or []
        messages.append({"role": "user", "content": body})

        api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
        client_ai = anthropic.Anthropic(api_key=api_key)
        system = (
            f"Sei l'assistente di {client.get('nome_azienda')} ({client.get('settore')}).\n"
            f"Stai raccogliendo info via SMS da un cliente che ha chiamato senza risposta.\n"
            f"Obiettivo: capire tipo lavoro e zona, raccogliere nome. Max 2 frasi stile SMS.\n"
            f"Quando hai nome + tipo lavoro: concludi confermando che il titolare ricontatterà presto.\n"
            f'OUTPUT solo JSON: {"{"}"reply":"...","qualified":false,"lead_name":null,"lead_type":null{"}"}'
        )
        resp = client_ai.messages.create(model="claude-haiku-4-5-20251001", max_tokens=150, system=system, messages=messages)
        text = resp.content[0].text.strip()
        s = text.find("{")
        e = text.rfind("}") + 1
        parsed = {"reply": "Grazie! Ti ricontatto presto.", "qualified": False, "lead_name": None, "lead_type": None}
        if s >= 0:
            try:
                parsed = json.loads(text[s:e])
            except Exception:
                pass

        reply = parsed.get("reply", "Grazie!")
        messages.append({"role": "assistant", "content": reply})
        update = {"messages": messages}

        if parsed.get("qualified"):
            update.update({"qualified": True, "lead_name": parsed.get("lead_name"),
                           "lead_type": parsed.get("lead_type"), "lead_phone": from_phone})
            owner_email = client.get("email_titolare")
            if owner_email:
                _send_plain_email(
                    owner_email,
                    f"Nuovo lead SMS: {parsed.get('lead_name') or from_phone}",
                    f"Lead da chiamata persa!\n\nNome: {parsed.get('lead_name')}\nTelefono: {from_phone}\nLavoro: {parsed.get('lead_type')}\n\nRicontattalo subito.",
                    sender_name="GetAutomatik"
                )
            send_telegram(f"Lead SMS!\nAzienda: {client.get('nome_azienda')}\nLead: {parsed.get('lead_name')} ({from_phone})\nLavoro: {parsed.get('lead_type')}")

        db.table("sms_conversations").update(update).eq("id", conv["id"]).execute()
        send_twilio_sms(from_phone, reply, twilio_number)
        return reply
    except Exception as ex:
        print(f"handle_sms_inbound error: {ex}")
        return "Grazie per il messaggio!"
