# n8n workflow portfolio

Four n8n workflows built to demonstrate patterns I use in production automation:
multi-source aggregation, LLM integration with structured output, stateful alerting
that doesn't cry wolf, and a WhatsApp AI assistant that is never allowed to invent a price.

Everything here runs locally and free — n8n self-hosted, Ollama for the model, public APIs
with no keys. Nothing in this repo touches any employer system or data.

---

## 1. Daily multi-source metrics digest

`workflows/01-daily-metrics-digest.json`

```
Schedule (08:00) → 2 APIs in parallel → Merge → Normalise → Compute deltas → LLM summary
```

Two APIs with completely different response shapes get flattened into one common record
(`{ source, metric, value }`), so everything downstream is shape-agnostic. Adding a third
source means adding one branch to the normaliser, not rewriting the pipeline.

**The design decision worth noting:** the workflow reports *deltas*, not absolutes. Previous
values live in n8n's workflow static data, and the model is told to lead with anything that moved
more than 10%. "1,240 stars" is a number. "+38, up 3.2%, first rise in four days" is a signal —
and a daily report nobody can act on gets filtered to an inbox folder within a fortnight.

In a real deployment the two sources would be GA4, Stripe, or the ad platforms. The pattern is
identical; only the endpoints change.

---

## 2. Webhook AI content pipeline with human approval

`workflows/02-ai-content-approval.json`

```
Webhook → Validate → [invalid → reject] → LLM generates 3 variants
        → Structured output parser → Parse-or-fail-safely → HOLD FOR HUMAN
```

A POST of `{ product, audience, tone }` produces three copy variants on different angles.

**Three things here are deliberate:**

- **Validation before the model call.** A malformed payload is rejected before it costs a
  model call. Validation is cheap; inference isn't.
- **A structured output parser, plus a `try/catch` behind it.** Forcing a JSON schema is the
  first line of defence; assuming it always works is how these break. If the parse fails the
  workflow returns a status, it doesn't throw.
- **A human gate before anything publishes.** Everything above the gate is generated; nothing
  below it runs until a person approves. An LLM does not get write access to a live channel
  unsupervised. This is the same instinct behind the 149 deterministic hooks I maintain around
  the AI tooling in my day job.

---

## 3. Stateful threshold alerting with auto-recovery

`workflows/03-stateful-threshold-alerting.json`

```
Schedule (hourly) → Fetch metric → Rolling-window state machine
                  → Switch: OPEN_ALERT / CLOSE_ALERT / NOOP
```

**This is the one I'd want to talk about.**

A naive alert fires the moment one reading crosses a threshold. One blip pages someone at 3am,
and within a week the alert is muted and the monitoring is worthless. This workflow holds a
rolling window of the last five readings and fires only when the condition *persists* —
three of the last five — then refuses to fire again until the metric has genuinely recovered,
at which point it closes its own ticket.

Three guarantees, all enforced in one Code node:

| Guarantee | How |
|---|---|
| **Persistence** — a transient spike never alerts | 3-of-5 window, not a single reading |
| **Idempotence** — an open alert is never duplicated | `alertOpen` flag suppresses re-firing |
| **Recovery** — the alert closes itself | full clean window → close, clear the flag |

### What building this actually took

The logic was the easy part. Getting it to *run* surfaced four separate defects, and **not one of
them showed as an error** — every execution reported "Workflow executed successfully":

1. **State in the wrong store.** The first version used `$getWorkflowStaticData('global')`, which
   n8n only persists for *production* executions. Manual runs got a fresh empty store every time,
   so the window never reached three and the workflow was structurally incapable of the behaviour
   it was written for. Green every run.

2. **A node emitting zero items.** On the first run the state file doesn't exist, so the read node
   returned nothing — and a node that emits zero items doesn't fail, it silently skips every node
   downstream. `alwaysOutputData` fixes it.

3. **Binary never reaching the Code node.** n8n runs Code in a separate JS task runner process, and
   binary buffers aren't passed into it. `$input.first().binary.data` was simply empty. The fix was
   to stop handling binary in code at all and use `Extract From File` / `Convert to File` either side.

4. **Parsed JSON nested one level down.** `Extract From File` puts the parsed content under
   `json.data`, not at the top level, so the state loader read `undefined` and fell back to an empty
   window. The loader now accepts either shape rather than depending on the default.

Every one of these was found by reading the persisted state directly — from n8n's SQLite database,
then from the state file on disk — rather than by trusting the execution log.

That is the whole reason I build automation the way I do. The failure mode that matters is not the
one that goes red; it is the workflow that reports success while doing nothing, because nobody
investigates a green run. Four of them in one workflow, in an afternoon.

I'd already built this pattern in code at work — Node.js with the sliding window in DynamoDB,
filing and closing Jira tickets off nightly test results. Rebuilding it in n8n was instructive:
the visual layer got me to a working version in about an hour, but state handling and idempotency
are still where these things break, and that's exactly where I'd drop back to a Code node rather
than trying to express it in nodes.

That's my honest read on n8n generally — excellent for orchestration and integration,
and I reach for code the moment correctness depends on state.

---

## Running these

```bash
# workflow 3 writes its state to ./.state, which n8n must be allowed to reach
N8N_RESTRICT_FILE_ACCESS_TO="$PWD/.state" npx n8n    # http://localhost:5678
ollama serve            # separate terminal
ollama pull llama3.2
```

Then **Workflows → Import from File** for each JSON, and add one Ollama credential
(base URL `http://localhost:11434`).

Workflow 3's two GitHub nodes are **disabled by default** — they need a credential and a real
repo. The state machine runs and routes correctly without them; enable them to have it actually
file tickets.

## Conventions used throughout

- Every node named for what it does, never `HTTP Request1`
- Retries and timeouts on every outbound HTTP call
- Credentials in n8n's credential store, never inline — nothing secret is in this repo
- Comments in the Code nodes explain *why*, not *what*

## A note on paths

Workflow 3 reads and writes `<repo>/.state/alert-state.json`. n8n requires an absolute path there,
so the two file nodes and the `N8N_RESTRICT_FILE_ACCESS_TO` variable need repointing at wherever you
clone this. `.state/` is gitignored — the state file is runtime data, not source.

---

## 4. WhatsApp AI order assistant for a small shop

`workflows/04-whatsapp-ai-order-assistant.json` · tests: `tests/test_04_whatsapp.py`

```
Meta webhook → Parse → Skip duplicates → text?  ─ yes → AI: classify + draft → Validate
                                               └ no  → hand to human ───────────┘
          → Reply to customer  +  Append to leads sheet  +  Notify owner (orders / needs human)
```

The use case is the most common one I see for Indonesian small businesses: customers order on
WhatsApp, the owner answers by hand, and leads get lost. A customer writes *"mau pesan 2 Arabika
Gayo, kirim ke Jl. Pandanaran 12"* and gets back the order, the total and the shipping cost in
seconds. The owner gets a WhatsApp ping with the order, and the row lands in the leads sheet.
Questions about opening hours or shipping are answered from the shop's info and don't bother
the owner.

It speaks the real WhatsApp Cloud API: Meta's GET verification handshake, the actual webhook
payload shape, and the Graph API `/messages` call for replies.

**The rules that make it safe to put in front of customers:**

| Rule | How |
|---|---|
| **The AI never does arithmetic** | It picks a product from an enum and a quantity. Code computes subtotal, shipping and the free-shipping threshold. |
| **The AI never invents a price** | Every `Rp` amount in a reply must exist in the price list, or the reply is replaced with a holding message and the owner is pinged. |
| **Nobody gets ghosted** | Model down, timeout, invalid JSON, unknown product: the customer still gets a polite reply, and the owner sees *why* the bot stepped back. |
| **One message, one reply** | Meta retries webhooks; message IDs are de-duplicated so a retry never sends a second reply or logs a second order. |
| **Receipts are not messages** | Delivery and read receipts arrive on the same webhook and are dropped before they reach the model. |
| **Photos and voice notes go to a human** | A text model doesn't guess at them. |

### What testing it found

The first version passed every check I had written, and it was still wrong. Reading the leads file
rather than trusting the test run showed three things:

1. **The model invented a shipping price.** Asked *"ongkir ke Jakarta berapa?"*, it replied
   Rp30.000. The real rate is a flat Rp15.000. It had also labelled the question as an order and
   paged the owner. This is why money is now computed in code and every quoted price is checked
   against the list. I confirmed the guard by swapping in a fake model that returns Rp30.000: the
   customer got the holding reply and the owner got *"AI quoted a price not in the price list:
   Rp30.000"*.
2. **Product, quantity and address came back empty.** A nested `order` object in the JSON schema was
   too much for a 3B model. A flat schema with the product as an enum fixed it.
3. **The sheet lost its header row when the file was recreated.** A "header already written" flag
   in workflow static data outlived the file. The workflow now checks whether the file exists.
   Reading a missing file gives zero items rather than an error, so the check is an expression on
   the file's binary data.

`tests/test_04_whatsapp.py` runs 24 end-to-end checks against the live workflow and a real local
model, with a mock Graph API standing in for WhatsApp so every outbound message is asserted on.
The checks cover the verify handshake, order totals, free shipping, missing address, duplicate
delivery, receipts, images, owner notifications and the price guard. The model is
non-deterministic, so the tests assert *behaviour* (answered vs. escalated) rather than the exact
label it chose. They pass on repeated runs.

### Running it

1. Import the workflow and fill in the **Config** node: products and prices, owner's number,
   WhatsApp Cloud API phone-number ID and token, model URL. Put the verify token in the
   **Token matches?** node as well.
2. Publish it, and point the Meta app's webhook at `https://<your-n8n>/webhook/whatsapp`.
3. For a client, the CSV branch becomes one Google Sheets **Append row** node with the same columns.

To run the tests locally: start Ollama (`llama3.2`) and n8n with
`N8N_RESTRICT_FILE_ACCESS_TO=~/n8n-files`, publish the workflow as shipped (it points at the mock
API on `127.0.0.1:8765`), then run `python3 tests/test_04_whatsapp.py`.

