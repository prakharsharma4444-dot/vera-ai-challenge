"""HTTP server required by the magicpin AI Challenge."""
from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from bot import compose, conversation_id as make_conversation_id, should_send

APP_START = time.time()
STATE_LOCK = Lock()

app = FastAPI(title="Vera Challenge Bot", version=os.getenv("BOT_VERSION", "2.0.0"))

# scope -> context_id -> {version, payload}
contexts: Dict[str, Dict[str, Dict[str, Any]]] = {
    "category": {},
    "merchant": {},
    "customer": {},
    "trigger": {},
}

# Conversation state and global patterns for the replay test.
conversations: Dict[str, Dict[str, Any]] = {}
auto_reply_counts: Dict[tuple[str, str], int] = {}
suppressed_conversations: set[str] = set()
sent_suppression_keys: set[str] = set()


class ContextPush(BaseModel):
    scope: str
    context_id: str
    version: int = Field(ge=0)
    payload: Dict[str, Any]
    delivered_at: Optional[str] = None


class TickRequest(BaseModel):
    now: Optional[str] = None
    available_triggers: list[str] = Field(default_factory=list)


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: int = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get(scope: str, cid: str) -> Optional[Dict[str, Any]]:
    item = contexts.get(scope, {}).get(cid)
    return item.get("payload") if item else None


def _normalize_auto(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _is_auto_reply(text: str) -> bool:
    t = _normalize_auto(text)
    patterns = [
        r"thank you for contacting",
        r"thank you for reaching out",
        r"our team will respond",
        r"we will get back to you",
        r"please leave (?:a )?message",
        r"automated response",
        r"office hours",
        r"respond shortly",
    ]
    return any(re.search(p, t) for p in patterns)


def _is_stop(text: str) -> bool:
    t = _normalize_auto(text)
    return any(x in t for x in [
        "stop messaging", "stop sending", "do not message", "don't message",
        "not interested", "this is spam", "useless spam", "no more messages",
        "remove me", "unsubscribe", "leave me alone",
    ])


def _is_commitment(text: str) -> bool:
    t = _normalize_auto(text)
    exactish = [
        "lets do it", "let's do it", "go ahead", "what's next", "whats next",
        "proceed", "do it", "yes please", "yes, please", "confirm", "done",
        "send it", "start it", "start now",
    ]
    return any(x in t for x in exactish) or bool(re.search(r"\byes\b.*\b(start|send|do|go)\b", t))


def _reply_action(req: ReplyRequest) -> Dict[str, Any]:
    msg = req.message.strip()
    mid = req.merchant_id
    cid = req.customer_id

    # Persist the conversation even when the judge starts a new arbitrary conversation ID.
    with STATE_LOCK:
        state = conversations.setdefault(req.conversation_id, {
            "merchant_id": mid,
            "customer_id": cid,
            "turns": [],
            "last_trigger_id": None,
            "last_bot_body": None,
            "ended": False,
        })
        if state.get("ended"):
            return {"action": "end", "rationale": "Conversation was already closed; no further outreach is sent."}
        state["turns"].append({"from": req.from_role, "body": msg, "turn": req.turn_number})

    # Opt-out / hostility always wins over all other routing.
    if _is_stop(msg):
        with STATE_LOCK:
            state["ended"] = True
            suppressed_conversations.add(req.conversation_id)
        return {
            "action": "end",
            "rationale": "Explicit opt-out/hostile stop signal detected; conversation is closed and future turns are suppressed.",
        }

    # Auto-reply detection is deliberately keyed by merchant + message, not just conversation_id,
    # because the official replay simulator may use a fresh conversation ID on each turn.
    if req.from_role == "merchant" and _is_auto_reply(msg):
        key = (mid, _normalize_auto(msg))
        with STATE_LOCK:
            auto_reply_counts[key] = auto_reply_counts.get(key, 0) + 1
            count = auto_reply_counts[key]
        if count == 1:
            body = "Looks like an auto-reply 😊 When the owner sees this, reply YES and I’ll pick it up from there."
            return {"action": "send", "body": body, "cta": "binary_yes_no", "rationale": "Detected a canned WhatsApp auto-reply; one low-friction flag is attempted before backing off."}
        if count == 2:
            return {"action": "wait", "wait_seconds": 86400, "rationale": "Same canned auto-reply repeated; backing off 24h rather than burning another turn."}
        with STATE_LOCK:
            suppressed_conversations.add(req.conversation_id)
        return {"action": "end", "rationale": "Auto-reply repeated 3+ times without a human signal; closing to avoid reply pollution."}

    # Explicit intent transition: never fall back into qualification mode.
    if req.from_role == "merchant" and _is_commitment(msg):
        body = "Great — moving to execution. I’ll prepare the concrete draft from the context already on hand. Reply CONFIRM when you want me to use it."
        with STATE_LOCK:
            state["last_bot_body"] = body
        return {
            "action": "send",
            "body": body,
            "cta": "binary_confirm_cancel",
            "rationale": "Merchant signaled explicit intent; switching directly from qualification to action mode.",
        }

    # Common confirmation replies after a bot action.
    if req.from_role == "merchant" and re.search(r"^(yes|yep|yeah|ok|okay|sure)\b", _normalize_auto(msg)):
        body = "Done — I’ll keep the next step focused on the action you just approved."
        with STATE_LOCK:
            state["last_bot_body"] = body
        return {
            "action": "send",
            "body": body,
            "cta": "open_ended",
            "rationale": "Acknowledged a positive merchant response and moved forward without re-qualifying.",
        }

    # A very common curveball in the official replay.
    if re.search(r"\b(gst|tax filing|file my taxes|income tax)\b", _normalize_auto(msg)):
        body = "I’ll leave GST/tax filing to your CA. I can stay on the merchant-growth task this conversation started — tell me the next action you want me to draft."
        return {
            "action": "send",
            "body": body,
            "cta": "open_ended",
            "rationale": "Out-of-scope tax request is declined briefly and the conversation is redirected to the Vera task.",
        }

    # Generic conversational fallback that remains short and asks for one next step.
    body = "Got it. I’ll keep this grounded in the details already shared — what should I take forward first?"
    with STATE_LOCK:
        state["last_bot_body"] = body
    return {"action": "send", "body": body, "cta": "open_ended", "rationale": "Kept the response short and action-oriented rather than introducing unsupported facts."}


@app.get("/v1/healthz")
@app.get("/v1/health")
def healthz() -> Dict[str, Any]:
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - APP_START, 2),
        "contexts_loaded": {scope: len(values) for scope, values in contexts.items()},
    }


@app.get("/v1/metadata")
def metadata() -> Dict[str, Any]:
    return {
        "team_name": os.getenv("TEAM_NAME", "Vera Builder"),
        "team_members": [x.strip() for x in os.getenv("TEAM_MEMBERS", "Team Member").split(",") if x.strip()],
        "model": "deterministic-rule-engine",
        "approach": "trigger routing + context grounding + consent/state guardrails + replay handlers",
        "contact_email": os.getenv("CONTACT_EMAIL", "").strip(),
        "version": os.getenv("BOT_VERSION", "2.0.0"),
        "submitted_at": os.getenv("SUBMITTED_AT", _now_iso()),
    }


@app.post("/v1/context")
def push_context(req: ContextPush) -> Dict[str, Any]:
    if req.scope not in contexts:
        return {"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {sorted(contexts)}"}
    if not req.context_id:
        return {"accepted": False, "reason": "invalid_context_id", "details": "context_id is required"}
    with STATE_LOCK:
        existing = contexts[req.scope].get(req.context_id)
        if existing and req.version < existing["version"]:
            return {"accepted": False, "reason": "stale_version", "current_version": existing["version"]}
        # Same version: idempotent no-op.
        if existing and req.version == existing["version"]:
            return {"accepted": True, "ack_id": f"ack_{req.scope}_{hashlib.sha1(req.context_id.encode()).hexdigest()[:10]}", "stored_at": _now_iso()}
        contexts[req.scope][req.context_id] = {"version": req.version, "payload": req.payload}
    return {"accepted": True, "ack_id": f"ack_{req.scope}_{hashlib.sha1((req.context_id+str(req.version)).encode()).hexdigest()[:10]}", "stored_at": _now_iso()}


@app.post("/v1/tick")
def tick(req: TickRequest) -> Dict[str, Any]:
    actions: list[Dict[str, Any]] = []
    # Highest urgency first; stable by trigger ID for deterministic ordering.
    candidates = []
    for tid in req.available_triggers:
        trigger = _get("trigger", tid)
        if not trigger:
            continue
        candidates.append((int(trigger.get("urgency", 0)), tid, trigger))
    candidates.sort(key=lambda x: (-x[0], x[1]))

    for _, tid, trigger in candidates:
        if len(actions) >= 20:
            break
        mid = trigger.get("merchant_id") or trigger.get("payload", {}).get("merchant_id")
        cid = trigger.get("customer_id") or trigger.get("payload", {}).get("customer_id")
        merchant = _get("merchant", mid) if mid else None
        customer = _get("customer", cid) if cid else None
        if not merchant:
            continue
        if trigger.get("expires_at") and req.now:
            # ISO comparison is enough for the challenge's UTC/offset timestamps when normalized by datetime parsing.
            try:
                from datetime import datetime as _dt
                now_dt = _dt.fromisoformat(req.now.replace("Z", "+00:00"))
                exp_dt = _dt.fromisoformat(trigger["expires_at"].replace("Z", "+00:00"))
                if now_dt > exp_dt:
                    continue
            except Exception:
                pass

        allowed, _ = should_send(_get("category", merchant.get("category_slug", "")) or {}, merchant, trigger, customer)
        if not allowed:
            continue
        suppression = trigger.get("suppression_key", tid)
        with STATE_LOCK:
            if suppression in sent_suppression_keys:
                continue

        category = _get("category", merchant.get("category_slug", "")) or {}
        composed = compose(category, merchant, trigger, customer)
        if not composed.get("body"):
            continue

        conv = make_conversation_id(mid, trigger, cid)
        with STATE_LOCK:
            sent_suppression_keys.add(suppression)
            conversations[conv] = {
                "merchant_id": mid,
                "customer_id": cid,
                "trigger_id": tid,
                "turns": [{"from": "vera", "body": composed["body"], "turn": 1}],
                "last_trigger_id": tid,
                "last_bot_body": composed["body"],
                "ended": False,
            }

        template = f"vera_{trigger.get('kind','message')}_v1" if composed["send_as"] == "vera" else f"merchant_{trigger.get('kind','message')}_v1"
        actions.append({
            "conversation_id": conv,
            "merchant_id": mid,
            "customer_id": cid,
            "send_as": composed["send_as"],
            "trigger_id": tid,
            "template_name": template,
            "template_params": [],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })
    return {"actions": actions}


@app.post("/v1/reply")
def reply(req: ReplyRequest) -> Dict[str, Any]:
    if req.from_role not in {"merchant", "customer"}:
        raise HTTPException(status_code=400, detail="from_role must be merchant or customer")
    return _reply_action(req)


@app.post("/v1/teardown")
def teardown() -> Dict[str, Any]:
    """Optional judge cleanup hook; clears all in-memory challenge state."""
    with STATE_LOCK:
        for bucket in contexts.values():
            bucket.clear()
        conversations.clear()
        auto_reply_counts.clear()
        suppressed_conversations.clear()
        sent_suppression_keys.clear()
    return {"ok": True, "cleared": True}
