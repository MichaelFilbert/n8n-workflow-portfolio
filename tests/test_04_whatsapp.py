"""End-to-end tests for workflow 04 (WhatsApp AI order assistant).

Runs against a live n8n with the workflow published, and a real local model (Ollama).
WhatsApp itself is replaced by a mock Graph API server started by this script, so every
message the workflow "sends" is captured and asserted on.

    python3 tests/test_04_whatsapp.py

Config: N8N_URL (default http://localhost:5678), LEADS_FILE (default ~/n8n-files/leads.csv).
The workflow's Config node must point waApiBase at http://127.0.0.1:8765/v21.0.
"""
import csv
import json
import re
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

N8N_URL = os.environ.get("N8N_URL", "http://localhost:5678")
WEBHOOK = f"{N8N_URL}/webhook/whatsapp"
LEADS_FILE = os.path.expanduser(os.environ.get("LEADS_FILE", "~/n8n-files/leads.csv"))
OWNER = "6281200000000"
VERIFY_TOKEN = "change-me-verify-token"
ALLOWED_PRICES = {"85.000", "60.000", "70.000", "15.000", "200.000"}

sent = []  # every request the workflow made to the "WhatsApp API"


class MockGraphAPI(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sent.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"messages": [{"id": "wamid.mock"}]}).encode())

    def log_message(self, *args):
        pass


def http(method, url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def meta_webhook(messages=None, statuses=None, name="Budi"):
    """The payload shape Meta's WhatsApp Cloud API actually POSTs."""
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "123456789012345"}}
    if messages:
        value["contacts"] = [{"wa_id": m["from"], "profile": {"name": name}} for m in messages]
        value["messages"] = messages
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "WABA_ID", "changes": [{"field": "messages", "value": value}]}]}


def text_msg(text, sender):
    return {"from": sender, "id": f"wamid.{uuid.uuid4().hex}", "timestamp": str(int(time.time())),
            "type": "text", "text": {"body": text}}


def to(number):
    return [s for s in sent if s["body"].get("to") == number]


def wait_for(cond, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(1)
    return False


def leads_rows_for(number):
    if not os.path.exists(LEADS_FILE):
        return []
    with open(LEADS_FILE, encoding="utf-8") as f:
        return [line for line in f if f",{number}," in line]


def lead(number):
    """The lead row for a customer, as a dict keyed by the CSV header."""
    if not os.path.exists(LEADS_FILE):
        return {}
    with open(LEADS_FILE, encoding="utf-8", newline="") as f:
        return next((r for r in csv.DictReader(f) if r["from"] == number), {})


results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n      {detail}" if detail and not ok else ""))


def new_customer():
    return "6289" + str(uuid.uuid4().int)[:9]


def main():
    server = HTTPServer(("127.0.0.1", 8765), MockGraphAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # 1. Meta's verification handshake
    code, body = http("GET", f"{WEBHOOK}?hub.mode=subscribe&hub.verify_token={VERIFY_TOKEN}&hub.challenge=12345")
    check("verify handshake returns the challenge", code == 200 and body == "12345", f"{code} {body!r}")
    code, _ = http("GET", f"{WEBHOOK}?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=12345")
    check("verify handshake with a wrong token is rejected (403)", code == 403, f"got {code}")

    # 2. An order: totals are computed in code, the owner is notified, a lead row is written
    buyer = new_customer()
    order = text_msg("Halo kak, saya mau pesan 2 Arabika Gayo 250g, kirim ke Jl. Pandanaran 12 Semarang", buyer)
    code, _ = http("POST", WEBHOOK, meta_webhook([order]))
    check("webhook answers 200 immediately", code == 200, f"got {code}")
    got = wait_for(lambda: to(buyer) and leads_rows_for(buyer))
    reply = to(buyer)[0]["body"] if to(buyer) else {}
    text = reply.get("text", {}).get("body", "")
    check("order: customer receives a reply", got and bool(text), str(reply))
    check("order: reply uses the WhatsApp API format",
          reply.get("messaging_product") == "whatsapp" and reply.get("type") == "text", str(reply))
    check("order: request carries the access token",
          bool(to(buyer)) and to(buyer)[0]["auth"] == "Bearer test-token")
    check("order: 2 x 85.000 + 15.000 ongkir = Rp185.000", "Rp185.000" in text and "Rp170.000" in text, text)
    row = lead(buyer)
    check("order: lead row has intent, product, quantity, address, total",
          row.get("intent") == "order" and row.get("product") == "Arabika Gayo 250g"
          and row.get("quantity") == "2" and "Pandanaran" in row.get("address", "") and row.get("total") == "185000",
          str(row))
    check("order: owner is notified with the total",
          wait_for(lambda: any(buyer in s["body"]["text"]["body"] and "185.000" in s["body"]["text"]["body"]
                               for s in to(OWNER)), 30),
          f"owner messages: {[s['body']['text']['body'][:80] for s in to(OWNER)]}")

    # 2b. Big order: free shipping kicks in; no address given, so the bot asks for it
    big = new_customer()
    http("POST", WEBHOOK, meta_webhook([text_msg("Mau order 3 Arabika Gayo 250g ya", big)]))
    wait_for(lambda: to(big))
    text = to(big)[0]["body"]["text"]["body"] if to(big) else ""
    check("order >= Rp200.000: free shipping, total Rp255.000", "GRATIS" in text and "Rp255.000" in text, text)
    check("order without address: asks for the address", "alamat" in text.lower(), text)
    check("order without address: owner still notified",
          wait_for(lambda: any(big in s["body"]["text"]["body"] for s in to(OWNER)), 30))

    # 3. A plain question: reply, but the owner is NOT bothered
    asker = new_customer()
    owner_before = len(to(OWNER))
    http("POST", WEBHOOK, meta_webhook([text_msg("Jam buka tokonya jam berapa ya kak?", asker)]))
    wait_for(lambda: to(asker) and leads_rows_for(asker))
    row = lead(asker)
    check("question: customer receives a reply", bool(to(asker)))
    # "question" and "other" are handled identically (answer, don't page the owner), and a
    # small model drifts between them — so assert the behaviour, not the label.
    check("question: answered by the bot, not escalated", row.get("intent") in ("question", "other")
          and row.get("needsHuman") == "false", str(row))
    time.sleep(3)
    check("question: owner is not notified", len(to(OWNER)) == owner_before,
          f"{len(to(OWNER)) - owner_before} extra owner message(s)")

    # 3b. A shipping question — the one the model first answered with an invented Rp30.000
    ship = new_customer()
    http("POST", WEBHOOK, meta_webhook([text_msg("Ongkir ke Jakarta berapa kak?", ship)]))
    wait_for(lambda: to(ship) and leads_rows_for(ship))
    text = to(ship)[0]["body"]["text"]["body"] if to(ship) else ""
    quoted = set(re.findall(r"Rp\s?(\d{1,3}(?:\.\d{3})*)", text))
    check("shipping question: not treated as an order", lead(ship).get("intent") != "order", str(lead(ship)))
    check("shipping question: no price outside the price list reaches the customer",
          quoted <= ALLOWED_PRICES, f"quoted {quoted}: {text}")

    # 4. Meta retries the same message: exactly one reply, one lead row
    retry_from = new_customer()
    msg = text_msg("Ongkir ke Jakarta berapa?", retry_from)
    http("POST", WEBHOOK, meta_webhook([msg]))
    wait_for(lambda: to(retry_from) and leads_rows_for(retry_from))
    http("POST", WEBHOOK, meta_webhook([msg]))
    time.sleep(8)
    check("duplicate delivery: replied exactly once", len(to(retry_from)) == 1, f"{len(to(retry_from))} replies")
    check("duplicate delivery: logged exactly once", len(leads_rows_for(retry_from)) == 1,
          f"{len(leads_rows_for(retry_from))} rows")

    # 5. Delivery/read receipts: nothing is sent, nothing is logged
    before = len(sent)
    http("POST", WEBHOOK, meta_webhook(statuses=[{"id": "wamid.x", "status": "read", "recipient_id": "628"}]))
    time.sleep(5)
    check("status-only webhook: nothing sent", len(sent) == before, f"{len(sent) - before} message(s) sent")

    # 6. A photo: no AI guess — holding reply, and the owner is asked to look
    photo_from = new_customer()
    photo = {"from": photo_from, "id": f"wamid.{uuid.uuid4().hex}", "timestamp": str(int(time.time())),
             "type": "image", "image": {"id": "media-1", "mime_type": "image/jpeg"}}
    http("POST", WEBHOOK, meta_webhook([photo]))
    wait_for(lambda: to(photo_from) and any(photo_from in s["body"]["text"]["body"] for s in to(OWNER)), 30)
    check("image: customer gets a holding reply", bool(to(photo_from)))
    check("image: owner is asked to handle it",
          any(photo_from in s["body"]["text"]["body"] and "PERLU DIBALAS" in s["body"]["text"]["body"]
              for s in to(OWNER)))
    check("image: logged as needs_human", lead(photo_from).get("intent") == "needs_human", str(lead(photo_from)))

    with open(LEADS_FILE, encoding="utf-8") as f:
        check("leads file starts with a header row", f.readline().startswith("receivedAt,from,name,intent"))

    server.shutdown()
    print(f"\n{sum(results)}/{len(results)} passed")
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
