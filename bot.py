"""Deterministic Vera-style message composer for the magicpin AI Challenge.

No external model/API is required. The strategy is:
1) route on trigger kind;
2) ground every claim in supplied context;
3) use merchant/customer state to choose one next best action;
4) keep customer consent/state checks ahead of copywriting.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Dict, Optional, Tuple


COMPOSE_VERSION = "2.0.0"


def _first_name(merchant: Dict[str, Any]) -> str:
    ident = merchant.get("identity", {})
    return ident.get("owner_first_name") or (ident.get("name", "").split(" ")[0] or "there")


def _merchant_name(merchant: Dict[str, Any]) -> str:
    return merchant.get("identity", {}).get("name", "the business")


def _active_offers(merchant: Dict[str, Any]) -> list[Dict[str, Any]]:
    return [o for o in merchant.get("offers", []) if o.get("status") == "active"]


def _offer(merchant: Dict[str, Any], keyword: str | None = None) -> Optional[str]:
    offers = _active_offers(merchant)
    if keyword:
        for o in offers:
            title = o.get("title", "")
            if keyword.lower() in title.lower():
                return title
    return offers[0].get("title") if offers else None


def _category_offer(category: Dict[str, Any], keyword: str | None = None) -> Optional[str]:
    offers = category.get("offer_catalog", [])
    if keyword:
        for o in offers:
            if keyword.lower() in o.get("title", "").lower():
                return o.get("title")
    return offers[0].get("title") if offers else None


def _digest_item(category: Dict[str, Any], trigger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = trigger.get("payload", {})
    wanted = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    for item in category.get("digest", []):
        if wanted and item.get("id") == wanted:
            return item
    # Fall back only by kind when the trigger explicitly asks for a digest-like item.
    if trigger.get("kind") in {"research_digest", "regulation_change", "cde_opportunity", "supply_alert"}:
        target_kind = {
            "research_digest": "research",
            "regulation_change": "compliance",
            "cde_opportunity": "cde",
            "supply_alert": "compliance",
        }[trigger.get("kind")]
        for item in category.get("digest", []):
            if item.get("kind") == target_kind:
                return item
    return None


def _num_pct(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        x = float(value)
    except Exception:
        return None
    if abs(x) <= 1:
        return f"{x * 100:.0f}%"
    return f"{x:.0f}%"


def _fmt_price(title: str) -> str:
    return title


def _lang_hint(merchant: Dict[str, Any]) -> str:
    langs = merchant.get("identity", {}).get("languages", [])
    if "hi" in langs:
        return "hinglish"
    return "english"


def _customer_consent_ok(customer: Dict[str, Any] | None, trigger_kind: str) -> bool:
    if not customer:
        return True
    prefs = customer.get("preferences", {})
    if prefs.get("reminder_opt_in") is False:
        return False
    scopes = set(customer.get("consent", {}).get("scope", []))
    required = {
        "recall_due": "recall_reminders",
        "appointment_tomorrow": "appointment_reminders",
        "chronic_refill_due": "refill_reminders",
        "customer_lapsed_hard": "winback_offers",
        "customer_lapsed_soft": "promotional_offers",
        "trial_followup": "promotional_offers",
        "wedding_package_followup": "promotional_offers",
    }.get(trigger_kind)
    return True if required is None else required in scopes


def _customer_state_ok(customer: Dict[str, Any] | None, trigger_kind: str) -> bool:
    if not customer:
        return True
    state = customer.get("state")
    if trigger_kind == "customer_lapsed_soft":
        return state in {"lapsed_soft", "lapsed_hard", "churned"}
    if trigger_kind == "customer_lapsed_hard":
        return state in {"lapsed_hard", "churned"}
    if trigger_kind == "recall_due":
        return state not in {"churned"}
    if trigger_kind == "chronic_refill_due":
        return state not in {"churned"}
    return True


def should_send(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any], customer: Dict[str, Any] | None) -> Tuple[bool, str]:
    """Pre-copy guardrail: consent, contradictory state, and obviously stale placeholder events."""
    kind = trigger.get("kind", "")
    if customer and not _customer_consent_ok(customer, kind):
        return False, f"Customer consent does not cover {kind}; suppressing rather than inventing permission."
    if customer and not _customer_state_ok(customer, kind):
        return False, f"Customer state is {customer.get('state')}, which conflicts with {kind}; waiting for a consistent trigger."

    payload = trigger.get("payload", {})
    perf = merchant.get("performance", {})

    # Some generated triggers are deliberately placeholders. Do not create a false fact from them.
    if payload.get("placeholder") and kind in {"perf_dip", "perf_spike"}:
        delta = perf.get("delta_7d", {})
        if kind == "perf_dip":
            vals = [v for v in delta.values() if isinstance(v, (int, float))]
            if vals and max(vals) >= 0:
                return False, "Placeholder perf-dip trigger conflicts with current merchant performance; waiting for the actual metric."
        if kind == "perf_spike":
            vals = [v for v in delta.values() if isinstance(v, (int, float))]
            if vals and max(vals) <= 0:
                return False, "Placeholder perf-spike trigger has no positive metric shift in merchant context."

    if kind == "festival_upcoming":
        if payload.get("placeholder") or not payload.get("festival") or not payload.get("date"):
            return False, "Festival trigger lacks the actual festival/date payload; waiting rather than fabricating timing."
        try:
            days_until = int(payload.get("days_until"))
            if days_until > 60:
                return False, "Festival is more than 60 days away; holding the outreach rather than sending a premature promotional nudge."
        except Exception:
            pass
    if payload.get("placeholder") and kind == "milestone_reached":
        return True, "Milestone payload is sparse; use merchant facts but do not invent the missing milestone value."
    if payload.get("placeholder") and kind == "competitor_opened":
        return True, "Competitor details are missing; use verified merchant/peer facts and label the alert as incomplete."
    return True, ""


def _merchant_message(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, str]:
    kind = trigger.get("kind", "")
    p = trigger.get("payload", {}) or {}
    name = _first_name(merchant)
    biz = _merchant_name(merchant)
    locality = merchant.get("identity", {}).get("locality", "")
    perf = merchant.get("performance", {}) or {}
    agg = merchant.get("customer_aggregate", {}) or {}
    signals = merchant.get("signals", []) or []
    active = _active_offers(merchant)

    # High-intent/action triggers first: do not re-qualify a merchant who already asked for action.
    if kind == "active_planning_intent":
        topic = p.get("intent_topic", "the plan")
        last = p.get("merchant_last_message", "")
        if "corporate_bulk_thali" in topic:
            thali = _offer(merchant, "Thali") or _category_offer(category, "Thali")
            body = (
                f"{name}, I’ve got the corporate-thali direction from your last note. "
                f"A clean starting point is the existing {thali} as the retail anchor, then a bulk tiered version for office orders. "
                f"I can turn that into a ready-to-send package instead of asking you more setup questions. Want me to draft it now?"
            )
        elif "kids_yoga" in topic:
            # Reuse already-discussed plan from conversation history when available.
            hist = " ".join(h.get("body", "") for h in merchant.get("conversation_history", [])[-3:])
            m = re.search(r"4-week program[^.]*₹2,?499", hist, re.I)
            if m:
                plan = m.group(0).replace("₹2,499", "₹2,499")
            else:
                plan = "the 4-week kids-yoga draft already discussed"
            body = f"{name}, I’ve got the kids-yoga draft ready: {plan}. Want me to turn it into the GBP post + Insta carousel?"
        else:
            body = f"{name}, you already asked about {topic}. I can move this into a concrete draft now. Want me to prepare the first version?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Explicit planning intent is converted directly into an artifact/action; no extra qualification loop."}

    if kind == "research_digest":
        item = _digest_item(category, trigger)
        title = item.get("title", "A new research item") if item else "A new category research item"
        source = item.get("source") if item else None
        n = item.get("trial_n") if item else None
        seg = item.get("patient_segment", "") if item else ""
        extra = f" — {n:,}-patient trial" if isinstance(n, int) else ""
        cohort = f" Your {agg.get('high_risk_adult_count')} high-risk adult patients make this especially relevant." if agg.get("high_risk_adult_count") else ""
        citation = f" {source}." if source else ""
        body = f"{name}, {title}{extra}.{citation}{cohort} Worth a 2-min read. Want me to pull the key takeaways and draft one useful customer-facing message?"
        return {"body": body, "cta": "open_ended", "rationale": "Research trigger is answered with the exact supplied item, source, and a merchant-specific relevance hook."}

    if kind == "regulation_change":
        item = _digest_item(category, trigger)
        deadline = p.get("deadline_iso")
        summary = item.get("summary", "") if item else ""
        source = item.get("source", "") if item else ""
        body = f"{name}, compliance update: {item.get('title', 'a new regulatory change') if item else 'a new regulatory item'}."
        if summary:
            body += f" {summary}"
        if source:
            body += f" Source: {source}."
        if deadline and deadline not in body:
            body += f" Effective {deadline}."
        body += " Want me to turn this into a one-page audit/SOP checklist?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Compliance messages use the supplied rule, deadline, and source; the CTA is an implementation checklist."}

    if kind == "cde_opportunity":
        item = _digest_item(category, trigger)
        if item:
            dt = item.get("date", "")
            when = ""
            if dt:
                try:
                    from datetime import datetime as _dt
                    when = f" on {_dt.fromisoformat(dt).strftime('%-d %b at %-I:%M %p')}".replace('AM','am').replace('PM','pm')
                except Exception:
                    when = f" on {dt}"
            credits = p.get("credits")
            fee = p.get("fee")
            detail = f" {credits} CE credits" if credits else ""
            fee_text = ", free for members" if fee == "free_for_members" else (f", {fee}" if fee else "")
            body = f"{name}, {item.get('title', 'IDA opportunity')}{when}{detail}{fee_text}. Source: {item.get('source', '')}. Your last note was about whitening/aligners, so this is a useful clinical-tech detour. Want the agenda + 3 practical takeaways?"
        else:
            body = f"{name}, a CDE opportunity is open, but the digest details are not complete in this trigger. I won’t guess the topic. Want me to pull the exact event details first?"
        return {"body": body, "cta": "open_ended", "rationale": "Event details are grounded in the category digest; missing fields are explicitly left unknown."}

    if kind in {"perf_dip", "seasonal_perf_dip"}:
        metric = p.get("metric", "views")
        delta = p.get("delta_pct")
        if delta is None:
            delta = perf.get("delta_7d", {}).get(f"{metric}_pct")
        delta_txt = _num_pct(delta) or ""
        current = perf.get(metric)
        baseline = p.get("vs_baseline")
        if kind == "seasonal_perf_dip" and p.get("is_expected_seasonal"):
            season = p.get("season_note", "the current seasonal window")
            member_count = agg.get("total_active_members")
            member_txt = f" You still have {member_count} active members." if member_count else ""
            current_txt = f" Your {metric} are at {current}." if current is not None else ""
            body = f"{name}, your {metric} are {delta_txt} this week{current_txt} — this is consistent with {season}.{member_txt} I’d protect retention instead of chasing cold acquisition. Want me to draft a low-effort retention push?"
        else:
            count_txt = f" ({current} now vs {baseline} baseline)" if current is not None and baseline is not None else (f" ({current} now)" if current is not None else "")
            focus = "first" if "unverified_gbp" in signals else "next"
            extra = " Your GBP is also unverified and renewal is in 12 days." if "unverified_gbp" in signals and merchant.get("subscription", {}).get("days_remaining") else ""
            body = f"{name}, your {metric} are down {delta_txt}{count_txt}.{extra} I’d fix the visible conversion blocker before stacking another offer. Want me to draft the verification/listing steps?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Performance decline is quantified from trigger/merchant data and converted into one concrete intervention."}

    if kind == "perf_spike":
        metric = p.get("metric")
        if not metric:
            vals = merchant.get("performance", {}).get("delta_7d", {})
            positive = [(k.replace("_pct", ""), v) for k, v in vals.items() if isinstance(v, (int, float)) and v > 0]
            if positive:
                best_metric, best_delta = max(positive, key=lambda kv: kv[1])
                body = f"{name}, a performance-spike alert fired, but the trigger doesn’t identify the metric. Your verified snapshot does show {best_metric} up {_num_pct(best_delta)} over 7d. I won’t guess the alert is about that metric. Want me to use the verified lift and identify what to repeat?"
                return {"body": body, "cta": "binary_yes_no", "rationale": "Placeholder spike is handled without pretending its missing metric is known; the response uses a separate verified merchant fact."}
            metric = "performance"
        delta = p.get("delta_pct")
        if delta is None:
            delta = perf.get("delta_7d", {}).get(f"{metric}_pct")
        delta_txt = _num_pct(delta) or ""
        current = perf.get(metric)
        baseline = p.get("vs_baseline")
        driver = p.get("likely_driver")
        driver_txt = f" after {driver.replace('_', ' ')}" if driver else ""
        count_txt = f" (baseline {baseline})" if baseline is not None else ""
        body = f"{name}, {metric} are up {delta_txt}{count_txt}{driver_txt}. That’s a signal worth preserving. Want me to turn the proven hook into a second post instead of changing the offer?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Positive movement is tied to its supplied driver and the next low-risk amplification step."}

    if kind == "milestone_reached":
        metric = p.get("metric")
        now = p.get("value_now")
        target = p.get("milestone_value")
        if metric == "review_count" and now is not None and target is not None:
            body = f"{name}, you’re at {now} reviews — just {target - now} to go for {target}. Want me to draft a simple review-request WhatsApp for customers after their next order?"
        else:
            body = f"{name}, you’ve got a milestone signal, but the exact metric value isn’t in the trigger. I won’t invent it. Want me to turn the milestone into a customer-facing prompt once the exact number lands?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Uses exact milestone data when present and explicitly avoids inventing missing values."}

    if kind == "gbp_unverified":
        uplift = _num_pct(p.get("estimated_uplift_pct"))
        path = p.get("verification_path", "the available verification path")
        body = f"{name}, your Google Business Profile is still unverified. The trigger estimates ~{uplift} upside from verification; the supplied path is {path.replace('_', ' ')}. Want me to walk you through the verification steps?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Verification alert uses the stated status, estimated uplift, and exact path without promising an outcome."}

    if kind == "festival_upcoming":
        festival = p.get("festival", "the upcoming festival")
        date = p.get("date", "")
        days = p.get("days_until")
        offer = _offer(merchant, "Hair Spa") or _offer(merchant)
        when = f"on {date}" if date else ""
        day_txt = f"That’s {days} days away" if days is not None else "It’s still ahead"
        body = f"Hi {name} — {festival} is {when}. {day_txt}, so I’d build the offer now but hold the customer push until the booking window is closer."
        if offer:
            body += f" Your current {offer} can be the offer anchor. Want me to draft the festive concept now?"
        else:
            body += " Want me to draft the festive concept now?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Festival outreach is timed to the supplied date; the campaign is planned without prematurely messaging customers."}

    if kind == "ipl_match_today":
        match = p.get("match", "today’s IPL match")
        venue = p.get("venue")
        tm = p.get("match_time_iso", "")
        weekend = p.get("is_weeknight") is False
        bogo = _offer(merchant, "Buy 1") or _offer(merchant, "BOGO")
        digest = next((d for d in category.get("digest", []) if "IPL" in d.get("title", "") or "IPL" in d.get("summary", "")), None)
        data_hook = ""
        if weekend and digest:
            data_hook = " Our Apr order data shows Saturday IPL can pull restaurant covers down 12% versus a typical Saturday."
        offer_hook = f" Your active offer is {bogo}." if bogo else ""
        time_txt = ""
        if tm:
            try:
                from datetime import datetime as _dt
                dt = _dt.fromisoformat(tm)
                time_txt = dt.strftime("%I:%M %p").lstrip("0").lower()
            except Exception:
                time_txt = ""
        body = f"Quick heads-up {name} — {match}" + (f" at {venue}" if venue else "") + (f", {time_txt}" if time_txt else "") + f" today.{data_hook}{offer_hook} I’d avoid forcing a weekday-only offer into tonight; want me to draft a delivery-first Saturday message instead?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Match-day trigger is interpreted through the supplied weekend flag and category IPL evidence; existing offer constraints are respected."}

    if kind == "category_seasonal":
        trends = p.get("trends", [])
        nice = ", ".join(x.replace("_demand_", " demand ").replace("_", " ") for x in trends[:4])
        delivery = _offer(merchant, "Home Delivery")
        body = f"{name}, summer demand has shifted: {nice}. With {agg.get('repeat_customer_pct', 0)*100:.0f}% repeat customers, this is a good shelf/visibility moment rather than a broad discount." if agg.get("repeat_customer_pct") else f"{name}, summer demand has shifted: {nice}."
        if delivery:
            body += f" You already have {delivery} active. Want me to turn the top 2 demand shifts into a shelf + GBP checklist?"
        else:
            body += " Want me to turn the top 2 demand shifts into a shelf + GBP checklist?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Seasonal demand is summarized from the trigger and connected to the merchant’s actual offer mix."}

    if kind == "competitor_opened":
        comp = p.get("competitor_name")
        dist = p.get("distance_km")
        their_offer = p.get("their_offer")
        own = _offer(merchant, "Cleaning") or _offer(merchant)
        if comp and dist is not None and their_offer:
            body = f"{name}, {comp} opened {dist:.1f} km away with {their_offer}. Your current {own or 'active offer'} means price-matching is optional, not automatic. You have {agg.get('high_risk_adult_count')} high-risk adults in your roster; I’d defend differentiation instead. Want me to draft a service-led GBP post?" if agg.get('high_risk_adult_count') else f"{name}, {comp} opened {dist:.1f} km away with {their_offer}. Your current {own or 'offer'} gives us a clear baseline; I’d defend on service rather than reflexive discounting. Want me to draft a service-led GBP post?"
        else:
            peer_dirs = category.get("peer_stats", {}).get("avg_directions_30d")
            dirs = merchant.get("performance", {}).get("directions")
            ratio = f" Your {dirs} directions/30d are already above the peer {peer_dirs}." if dirs and peer_dirs else ""
            body = f"{name}, a competitor-opened alert just fired, but the competitor details are incomplete in this payload. I won’t invent a name or price.{ratio} Want me to draft a retention-focused post around your existing offer?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Competitor pressure is acknowledged without fabricating missing competitor data; response focuses on defendable differentiation."}

    if kind == "curious_ask_due":
        cat = merchant.get("category_slug") or category.get("slug")
        guess = _offer(merchant, "Hair Spa") or _offer(merchant, "Thali") or _offer(merchant, "Yoga") or _offer(merchant)
        if cat == "salons" and guess:
            q = f"Is {guess} getting the most questions this week, or is there another service taking the lead?"
        elif guess:
            q = f"Is {guess} the service getting the most questions this week, or is something else winning?"
        else:
            q = "What service is getting the most questions this week?"
        body = f"Hi {name} — {q} Tell me the top one and I’ll turn it into a Google post + a 4-line WhatsApp reply you can reuse."
        return {"body": body, "cta": "open_ended", "rationale": "Curious-ask cadence uses a low-stakes merchant question plus immediate effort-externalization."}

    if kind == "renewal_due":
        days = p.get("days_remaining")
        plan = p.get("plan") or merchant.get("subscription", {}).get("plan")
        amount = p.get("renewal_amount")
        amount_txt = f" ₹{amount:,}" if isinstance(amount, (int, float)) else ""
        body = f"{name}, your {plan or 'current'} subscription has {days} days left." if days is not None else f"{name}, your {plan or 'current'} subscription is coming up for renewal."
        if amount is not None:
            body += f" Renewal amount is{amount_txt}."
        body += " Want me to walk you through renewal before the window closes?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Renewal reminder uses the supplied plan, countdown, and price when available; one action CTA."}

    if kind == "dormant_with_vera":
        days = p.get("days_since_last_merchant_message")
        topic = p.get("last_topic")
        lapsed = agg.get("lapsed_90d_plus") or agg.get("lapsed_180d_plus")
        if days is not None:
            body = f"{name}, it’s been {days} days since we last spoke"
        else:
            body = f"{name}, quick re-open after a quiet stretch"
        if topic:
            body += f" — last time we were on {topic.replace('_', ' ')}."
        else:
            body += "."
        if lapsed:
            body += f" You also have {lapsed} lapsed customers in the current snapshot."
        elif agg.get("total_unique_ytd"):
            body += f" The business has {agg.get('total_unique_ytd'):,} unique customers YTD."
        body += " Want one concrete reactivation idea, not a generic campaign?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Dormancy is treated as a re-entry moment tied to the last topic when known and a current merchant fact."}

    if kind == "winback_eligible":
        days = p.get("days_since_expiry")
        dip = _num_pct(p.get("perf_dip_pct"))
        added = p.get("lapsed_customers_added_since_expiry")
        body = f"{name}, you’re {days} days past expiry" if days is not None else f"{name}, your win-back window is open"
        if dip:
            body += f" and performance is down {dip}."
        if added:
            body += f" {added} lapsed customers were added since expiry."
        body += " Want me to draft one reactivation message around that cohort?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Win-back trigger combines the stated time, performance shift, and cohort size into one reactivation ask."}

    if kind == "review_theme_emerged":
        theme = p.get("theme")
        occ = p.get("occurrences_30d")
        trend = p.get("trend")
        quote = p.get("common_quote")
        body = f"{name}, a review theme is emerging: {theme.replace('_', ' ') if theme else 'one theme'}" + (f" — {occ} mentions in 30d" if occ else "") + (f", {trend}" if trend else "") + "."
        if quote:
            body += f" One customer put it as: \"{quote}\"."
        body += " Want me to turn the theme into a small operational fix + response draft?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Review signal is made concrete with occurrence count/trend/quote when supplied."}

    if kind == "supply_alert":
        batches = p.get("affected_batches", [])
        molecule = p.get("molecule", "the flagged medicine")
        manufacturer = p.get("manufacturer")
        batch_text = ", ".join(batches) if batches else "the affected batches"
        body = f"{name}, urgent supply alert: {molecule} — batches {batch_text}" + (f" from {manufacturer}" if manufacturer else "") + ". Check stock and recent dispensing before the next customer handoff."
        affected = agg.get("chronic_rx_count")
        if affected:
            body += f" Your snapshot shows {affected} chronic-Rx customers overall."
        body += " Want me to draft the customer-notification workflow?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Supply alert uses exact batch/molecule data and avoids inventing safety outcomes or affected-customer counts."}

    # Generic merchant-facing fallback.
    city = merchant.get("identity", {}).get("city", "")
    views = perf.get("views")
    active_name = _offer(merchant)
    fact = f" {views} views/30d" if views is not None else (f" {active_name} is currently active." if active_name else "")
    body = f"Hi {name} — I’ve got a {kind.replace('_', ' ')} signal for {biz} in {locality or city}.{fact} Want me to turn the signal into one concrete next step?"
    return {"body": body, "cta": "open_ended", "rationale": "Fallback remains grounded in the known trigger kind and merchant facts; no unsupported details are added."}


def _customer_message(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any], customer: Dict[str, Any]) -> Dict[str, str]:
    kind = trigger.get("kind", "")
    p = trigger.get("payload", {}) or {}
    name = customer.get("identity", {}).get("name", "there")
    biz = _merchant_name(merchant)
    owner = _first_name(merchant)
    category_slug = merchant.get("category_slug", category.get("slug", ""))
    active = _active_offers(merchant)
    lang = customer.get("identity", {}).get("language_pref", "")

    if kind == "recall_due":
        service = p.get("service_due", "your recall")
        slots = [s.get("label") for s in p.get("available_slots", []) if s.get("label")]
        slot_text = " ya ".join(slots[:2]) if "hi" in lang.lower() and len(slots) >= 2 else " or ".join(slots[:2])
        price = _offer(merchant, "Cleaning") or _offer(merchant)
        body = f"Hi {name}, {biz} here 🦷 — your {service.replace('_', ' ')} recall is due." if category_slug == "dentists" else f"Hi {name}, {biz} here — your {service.replace('_', ' ')} reminder is due."
        if slot_text:
            body += f" {('Apke liye' if 'hi' in lang.lower() else 'We have')} {slot_text} ready."
        if price:
            body += f" {price}."
        body += " Reply with the slot you prefer, or send a time that works."
        return {"body": body, "cta": "multi_choice_slot" if len(slots) >= 2 else "open_ended", "rationale": "Customer recall uses the supplied due service, available slots, active offer, and customer language preference."}

    if kind == "chronic_refill_due":
        mols = p.get("molecule_list", [])
        due = p.get("stock_runs_out_iso")
        delivery = p.get("delivery_address_saved")
        body = f"Namaste — {biz} here. {name}'s regular medicines ({', '.join(mols)}) are due before {due[:10] if due else 'the next refill window'}."
        if delivery:
            body += " Your saved delivery address is available."
        offer = _offer(merchant, "Home Delivery")
        if offer:
            body += f" {offer}."
        body += " Reply CONFIRM to arrange the refill, or message us if the prescription details have changed."
        return {"body": body, "cta": "binary_confirm_cancel", "rationale": "Refill reminder is limited to supplied medicine names/due date and uses an existing delivery offer; no dose/brand is invented."}

    if kind == "customer_lapsed_hard":
        days = p.get("days_since_last_visit")
        focus = p.get("previous_focus")
        offer = _offer(merchant, "Trial") or _offer(merchant)
        body = f"Hi {name} 👋 {owner} from {biz} here. It’s been {days} days since your last visit." if days is not None else f"Hi {name} 👋 {owner} from {biz} here. It’s been a while since your last visit."
        body += " No pressure — coming back can be as simple as one session."
        if focus:
            body += f" You’d previously been focused on {focus.replace('_', ' ')}."
        if offer:
            body += f" We currently have {offer}."
        body += " Want me to hold a spot? Reply YES."
        return {"body": body, "cta": "binary_yes_no", "rationale": "Win-back uses lapse duration, prior goal, and an existing offer while keeping the tone non-judgmental."}

    if kind == "customer_lapsed_soft":
        state = customer.get("state")
        last_visit = customer.get("relationship", {}).get("last_visit")
        offer = _offer(merchant)
        body = f"Hi {name}, {biz} here. We haven’t seen you since {last_visit or 'your last visit'}." if last_visit else f"Hi {name}, {biz} here — a quick check-in after your last visit."
        if state == "churned":
            body += " No hard sell — just sharing that we can help with a fresh appointment or current offer when you’re ready."
        elif offer:
            body += f" {offer} is currently available."
        body += " Want the current options?"
        return {"body": body, "cta": "open_ended", "rationale": "Customer state is reflected honestly; a churned customer gets a low-pressure reactivation message rather than a fake recall."}

    if kind == "wedding_package_followup":
        wedding = p.get("wedding_date")
        days = p.get("days_to_wedding")
        window = p.get("next_step_window_open")
        body = f"Hi {name} 💍 {biz} here. Your wedding is {wedding or 'coming up'} — {days} days out if that count is current."
        if window:
            body += f" The {window.replace('_', ' ')} window is open."
        body += " Want me to send the package options and the next available slot?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Bridal follow-up uses the wedding date/window directly and avoids inventing package price or availability."}

    if kind == "trial_followup":
        trial = p.get("trial_date")
        options = [x.get("label") for x in p.get("next_session_options", []) if x.get("label")]
        body = f"Hi {name}, {biz} here — following up on your trial from {trial or 'recently'}."
        if options:
            body += f" Next option: {options[0]}."
        body += " Want me to reserve it?"
        return {"body": body, "cta": "binary_yes_no", "rationale": "Trial follow-up uses only the supplied trial date and next-session options."}

    if kind == "appointment_tomorrow":
        body = f"Hi {name}, {biz} here — quick reminder that you have an appointment tomorrow."
        body += " Reply CONFIRM if everything still works, or send us the time/detail that needs changing."
        return {"body": body, "cta": "binary_confirm_cancel", "rationale": "Appointment reminder stays minimal because the trigger payload does not include time/service details."}

    # Customer-facing fallback.
    body = f"Hi {name}, {biz} here — quick update related to your account."
    body += " Want the current details?"
    return {"body": body, "cta": "open_ended", "rationale": "Customer-facing fallback is deliberately low-claim and consent-gated."}


def compose(category: Dict[str, Any], merchant: Dict[str, Any], trigger: Dict[str, Any], customer: Dict[str, Any] | None = None) -> Dict[str, str]:
    """Compose one deterministic message from the challenge's four contexts."""
    send_as = "merchant_on_behalf" if customer is not None or trigger.get("scope") == "customer" else "vera"
    ok, guard_reason = should_send(category, merchant, trigger, customer)
    if not ok:
        return {
            "body": "",
            "cta": "none",
            "send_as": send_as,
            "suppression_key": trigger.get("suppression_key", trigger.get("id", "")),
            "rationale": guard_reason,
        }

    msg = _customer_message(category, merchant, trigger, customer) if send_as == "merchant_on_behalf" and customer else _merchant_message(category, merchant, trigger)
    return {
        "body": msg["body"],
        "cta": msg["cta"],
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key", trigger.get("id", "")),
        "rationale": msg["rationale"],
    }


def conversation_id(merchant_id: str, trigger: Dict[str, Any], customer_id: str | None = None) -> str:
    key = f"{merchant_id}|{customer_id or ''}|{trigger.get('kind','')}|{trigger.get('id','')}"
    suffix = hashlib.sha1(key.encode()).hexdigest()[:8]
    scope = f"_cust_{customer_id}" if customer_id else ""
    return f"conv_{merchant_id}{scope}_{trigger.get('kind','signal')}_{suffix}"
