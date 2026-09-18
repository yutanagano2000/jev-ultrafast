"""TypeSafe makes choices; an optional small OpenAI-compatible model writes field values."""

import datetime
import json
import math
import os
import re
import time

import httpx

from .questions import NEXT_ACTION, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)


def post_json(url, key, body):
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def candidate_values(data, field):
    """Deterministically narrow provided values by the field's declared format
    (label wording, maxlength) before asking the model. A phone-shaped field
    can only take a phone-shaped value; a maxlength drops values that cannot
    physically fit. Returns the surviving values, or all values when no
    format hint applies."""
    label = field.get("label") or ""
    values = list(dict.fromkeys(str(v) for v in data.values()))
    filters = [
        (r"電話|tel|fax", r"0\d{1,4}[-‐−―ー]?\d{1,4}[-‐−―ー]?\d{3,4}"),
        (r"郵便|〒", r"\d{3}-?\d{4}"),
        (r"カナ|フリガナ|ふりがな", r"[ァ-ヶー\s　]+"),
        (r"メール|email|e-mail", r"\S+@\S+"),
        # Street-number style fields take a digits-hyphen-digits shape.
        (r"番地|地番|丁目", r"[\d０-９]+[-‐−―ー][\d０-９]+"),
    ]
    if re.search(r"日付|年月日|ご希望日|予定日|date", label, re.IGNORECASE):
        # Date-shaped field: no profile value can satisfy it, so synthesize a
        # plausible near-future date (mock values are acceptable for required
        # fields). Slash form matches yyyy/mm/dd masks.
        future = (datetime.date.today() + datetime.timedelta(days=30)).strftime("%Y/%m/%d")
        return [future] + [
            v for v in values if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", v)
        ]
    for field_re, value_re in filters:
        if re.search(field_re, label, re.IGNORECASE):
            narrowed = [v for v in values if re.fullmatch(value_re, v)]
            if narrowed:
                return narrowed
    maxlength = field.get("maxlength")
    if isinstance(maxlength, int) and 0 < maxlength:
        part = re.search(r"\((\d+)/(\d+)\)\s*$", label)
        if part:
            # Split segment: only the n-th '-'-separated part must fit.
            index = int(part.group(1))
            narrowed = [
                v for v in values
                if len(re.split(r"[-‐−―ー]", v)) == int(part.group(2))
                and len(re.split(r"[-‐−―ー]", v)[index - 1]) <= maxlength
            ]
        else:
            narrowed = [v for v in values if len(v) <= maxlength]
        if narrowed:
            return narrowed
    return values


def filtered_actions(state, history, data=None):
    """Apply the run's suppression rules to the observed action space.

    Returns only the actions the policy still considers legal: settled or
    unfillable fills removed, parked labels removed (loop killers, regression
    clicks, alert-only openers, toggles, route closures), exhausted errored
    fills removed. Both the small model and the rescue path must choose from
    this list — offering parked actions to the rescue would resurrect the
    exact loops suppression exists to kill."""
    unfillable = {
        re.sub(r"\s+", "", h["action"])
        for h in history
        if h.get("outcome") == "no_value"
    }
    # A field that already received its value (or already held it) is settled;
    # re-offering it only invites re-typing loops when stale error text remains.
    settled = {
        re.sub(r"\s+", "", h["action"])
        for h in history
        if h.get("kind") == "fill" and h.get("outcome") in {"value_set", "unchanged"}
    }
    norm = lambda s: re.sub(r"\s+", "", s or "")  # noqa: E731
    weak = {"unchanged", "same_url_changed", "no_value", "dialog_opened", "regressed", "stale"}
    cur_errsig = "|".join(
        sorted((state.get("errors") or []) + (state.get("required_empty") or []))
    )
    # Route closure: an action that failed twice under the SAME unresolved
    # error signature cannot succeed until that state changes. Park it while
    # the signature holds; it reopens automatically once errors are fixed.
    parked = set()
    if cur_errsig:
        for label in {norm(h["action"]) for h in history[-15:]}:
            tries = [h for h in history if norm(h["action"]) == label]
            if (
                len(tries) >= 2
                and all(t.get("outcome") in weak for t in tries[-2:])
                and all(t.get("errsig") == cur_errsig for t in tries[-2:])
            ):
                parked.add(label)
    # Streak parking: three identical weak outcomes in a row parks the action
    # for one decision, forcing a detour (scroll, fix another field).
    if (
        len(history) >= 3
        and len({norm(h["action"]) for h in history[-3:]}) == 1
        and all(h.get("outcome") in weak for h in history[-3:])
    ):
        parked.add(norm(history[-1]["action"]))
    # When the trajectory itself is stalling (progress Score at the lowest
    # level on the last decision), widen suppression to every action with >=3
    # cumulative weak outcomes. Errored fields stay fillable: they are the
    # way out.
    stalling = (
        history and (history[-1].get("progress") is not None)
        and history[-1]["progress"] < 1
    )
    if stalling:
        counts = {}
        for h in history:
            if h.get("outcome") in weak:
                counts[norm(h["action"])] = counts.get(norm(h["action"]), 0) + 1
        parked |= {label for label, n in counts.items() if n >= 3}
    # Dialog-openers that only ever produce an ALERT (lookup buttons that
    # complain and change nothing): one open is a fair try, two is a loop.
    # Confirms are real gateways to progress, so they do not count.
    opens = {}
    for i, h in enumerate(history):
        if h.get("outcome") != "dialog_opened" or i + 1 >= len(history):
            continue
        if "alert" in (history[i + 1].get("action") or "").lower():
            opens[norm(h["action"])] = opens.get(norm(h["action"]), 0) + 1
    parked |= {label for label, n in opens.items() if n >= 2}
    # Regression clicks: an action that undid confirmed work (fewer locked
    # controls after it ran) is parked for the rest of the run. Re-editing a
    # confirmed stage is a rare repair need; letting it repeat is the proven
    # loop — a wrongly confirmed value surfaces as a field-level error whose
    # repair path is a fresh select, not another unlock click.
    parked |= {
        norm(h["action"]) for h in history if h.get("outcome") == "regressed"
    }
    # Regression navigation: an action that lands on a page state already
    # visited (same fingerprint) goes backward — park it so the model cannot
    # keep detouring through menus it has already escaped.
    seen_fp = set()
    for h in history:
        fp = h.get("fp")
        if fp and fp in seen_fp and h.get("outcome") == "navigated":
            parked.add(norm(h["action"]))
        if fp:
            seen_fp.add(fp)
    # Collapsible-region toggles ping-pong by definition: two weak outcomes
    # (open then close, or vice versa) is a loop, so park the trigger.
    for a in state["actions"]:
        if a.get("toggle"):
            n = sum(
                1
                for h in history
                if norm(h["action"]) == norm(a.get("label"))
                and h.get("outcome") in weak
            )
            if n >= 2:
                parked.add(norm(a.get("label")))
    last_outcome = {}
    for h in history:
        last_outcome[norm(h["action"])] = h.get("outcome")
    # Exhausted errored fields: every data candidate was already injected and
    # rejected AND the placeholder was tried too — nothing left to type, so
    # stop offering the field instead of looping on it.
    exhausted = set()
    for a in state["actions"]:
        if a.get("kind") == "fill" and a.get("error") and data:
            ln = norm(a.get("label"))
            tried = {
                str(h["fill_source"])
                for h in history
                if h.get("kind") == "fill"
                and norm(h["action"]) == ln
                and h.get("fill_source")
            }
            placeholder_tried = any(
                h.get("kind") == "fill"
                and norm(h["action"]) == ln
                and h.get("text_helper") == "placeholder"
                for h in history
            )
            same_sig_tries = sum(
                1
                for h in history
                if h.get("kind") == "fill"
                and norm(h["action"]) == ln
                and h.get("errsig") == cur_errsig
            )
            if (
                placeholder_tried
                and all(str(c) in tried for c in candidate_values(data, a))
            ) or same_sig_tries >= 3:
                exhausted.add(ln)
    if state.get("overlay"):
        # A stuck full-viewport mask swallows every hit test — clicks cannot
        # reach anything until it is dismissed, so they are not legal choices.
        return [
            a for a in state["actions"]
            if a["kind"] in {"dismiss", "wait", "dialog"}
        ]
    actions = [
        a for a in state["actions"]
        # A settled field flagged with an error is re-offered only when the
        # error signature changed since its last fill — otherwise retyping
        # the same value under the same error state cannot repair anything.
        if not (
            a["kind"] == "fill"
            and (
                # no_value is definitive: the field could not be filled.
                norm(a.get("label")) in unfillable
                or norm(a.get("label")) in exhausted
                # Errored fields stay fillable: value rotation (below) injects
                # a different candidate, which is the repair path.
                or (norm(a.get("label")) in settled and not a.get("error"))
                # A field that already holds a value and shows no error is
                # satisfied — re-typing it can only churn.
                or (a.get("value") and not a.get("error"))
            )
        )
        # Parked applies even to errored fields once re-typing produced
        # "unchanged": re-entering the same value cannot repair the error.
        # The bypass is for fill fields only — an error flag on a click
        # target (e.g. a section header whose label absorbed error text)
        # must not resurrect a parked loop.
        and (
            norm(a.get("label")) not in parked
            or (
                a["kind"] == "fill"
                and a.get("error")
                and last_outcome.get(norm(a.get("label"))) != "unchanged"
            )
        )
    ]
    return actions


def choose(state, goal, history, data=None):
    actions = filtered_actions(state, history, data)
    elements, targets, controls = action_space(actions)
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets if targets[key]}
    operations.update({key: value["label"] for key, value in controls.items()})
    if not state.get("dialog") and not (state.get("required_empty") or []):
        # DONE is provably false while required fields stand empty, and BLOCKED
        # is premature while unfinished work still offers legal actions — both
        # are only honest once the required list is clean (and never while a
        # native dialog is pending, where accept/dismiss is the only answer).
        operations.update(
            DONE="Every requirement is visibly satisfied.",
            BLOCKED="No supported operation can progress.",
        )
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{
                        k: a[k]
                        for k in (
                            "role", "checked", "selected", "expanded",
                            "required", "maxlength", "pattern", "error",
                        )
                        if k in a
                    },
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    # Semantic verdicts ride along in the same request, like the Mario policy's
    # jump_needed/danger questions. goal_met cross-checks DONE; field_source
    # maps the chosen text field onto a profile key so values are injected
    # deterministically instead of generated by a text model.
    questions["goal_met"] = {
        "type": "noul",
        "instructions": (
            "Is every requirement of the goal already visibly satisfied on this page? "
            "Answer yes only when the page itself shows the required end state."
        ),
    }
    # Trajectory grading, same idea as the Mario policy's danger score: when
    # the recent action log is a no-progress loop, the next decision tightens
    # suppression of repeat weak outcomes.
    questions["progress"] = {
        "type": "score",
        "criteria": [
            "Stalled: repeated no-effect actions or the same validation errors",
            "Partial: some useful actions mixed with detours",
            "Clear forward progress toward the goal",
        ],
        "instructions": (
            "How much did recent_actions advance the goal? Stalled means loops of "
            "actions that changed nothing or re-hit the same validation errors."
        ),
    }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {
                k: state[k]
                for k in ("url", "title", "text", "locked", "fields", "errors", "required_empty", "dialog")
                if k in state
            },
            "data": data or {},
            "unfillable": sorted(
                {h["action"] for h in history if h.get("outcome") == "no_value"}
            ),
            # An action repeated twice with no progress is discouraged once, so
            # the model tries an alternative (e.g. fixing an errored field)
            # instead of thrashing the same button. The streak resets as soon
            # as a different action runs, so the action is not parked forever.
            "discouraged": (
                [history[-1]["action"]]
                if len(history) >= 2
                and history[-1].get("action") == history[-2].get("action")
                and history[-1].get("outcome") in {"unchanged", "same_url_changed", "no_value"}
                and history[-2].get("outcome") in {"unchanged", "same_url_changed", "no_value"}
                else []
            ),
            "elements": elements,
            "recent_actions": [
                {
                    k: h.get(k)
                    for k in ("action", "kind", "text", "page_changed", "outcome", "detail")
                    if h.get(k) is not None
                }
                for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    result = post_json("https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        if operation not in controls and operation not in {"DONE", "BLOCKED"}:
            raise RuntimeError("Model chose an operation with no valid targets")
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]

    answers = result["answers"]
    fill_source = None
    fill_candidates = []
    if data and operation == "TYPE_TEXT" and target is not None:
        # Second focused question: now that the target field is known, ask which
        # provided value fits it. Deterministic format narrowing (label wording,
        # maxlength) removes candidates that cannot physically apply before the
        # model chooses among the rest.
        field = targets[operation][target]
        fill_candidates = candidate_values(data, field)
        # Value rotation on error: every value already injected into this
        # field was rejected — drop them all so the model must pick a fresh
        # candidate (or, once exhausted, the placeholder path) instead of
        # ping-ponging between two wrong values.
        if field.get("error"):
            label_norm = re.sub(r"\s+", "", field.get("label") or "")
            tried = {
                str(h["fill_source"])
                for h in history
                if h.get("kind") == "fill"
                and re.sub(r"\s+", "", h["action"]) == label_norm
                and h.get("fill_source")
            }
            if tried:
                fill_candidates = [v for v in fill_candidates if v not in tried]
            # Every candidate plus the placeholder already failed on this
            # field — nothing left to inject, so stop offering it.
        if len(fill_candidates) == 1:
            fill_source = fill_candidates[0]
        else:
            source_body = {
                "model": body["model"],
                "state": {
                    "page": {k: state[k] for k in ("url", "title") if k in state},
                    "field": {
                        "label": field["label"],
                        "current_value": field.get("value", ""),
                        **{k: field[k] for k in ("required", "maxlength", "pattern") if k in field},
                    },
                    "goal": goal,
                },
                "questions": {
                    "field_source": {
                        "type": "choice",
                        "criteria": {
                            **{
                                v: {"key": next(k for k, val in data.items() if str(val) == v)}
                                for v in fill_candidates
                            },
                            "none": "No provided value fits this field.",
                        },
                        "instructions": (
                            "Which provided value belongs in this field? Each option is a "
                            "value, described by its data key. Match the field meaning to "
                            "the key. Judge from the field's label — current_value may hold "
                            "a mistyped leftover and is not evidence. "
                            "Answer 'none' only when no key fits."
                        ),
                    }
                },
            }
            try:
                source_result = post_json(
                    "https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], source_body
                )
            except RuntimeError:
                source_result = {}
            fill_answer = (source_result.get("answers") or {}).get("field_source") or {}
            picked = fill_answer.get("choice")
            if picked in fill_candidates:
                fill_source = str(picked)
    goal_answer = answers.get("goal_met") or {}
    progress_answer = answers.get("progress") or {}
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "fill_source": fill_source,
        "fill_candidates": fill_candidates,
        "goal_met": goal_answer.get("noul"),
        "progress": progress_answer.get("score"),
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def rescue_decision(state, goal, history, data=None):
    """Stall escalation: when the small model keeps picking weak actions, ask a
    stronger OpenAI-compatible model for ONE choice from the same observed
    action space. Values are never sent — labels, flags and outcomes only."""
    key = os.environ.get("RESCUE_MODEL_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    base = os.environ.get("RESCUE_MODEL_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = os.environ.get("RESCUE_MODEL", "deepseek/deepseek-v4.1-flash")
    legal = filtered_actions(state, history, data)
    acts = [
        {
            "id": a["id"],
            "kind": a["kind"],
            "label": a.get("label"),
            **{k: a[k] for k in ("required", "error", "dialog", "toggle") if a.get(k)},
            **({"options_target": a["value"]} if a["kind"] == "select" else {}),
        }
        for a in legal
    ]
    if not acts:
        return None
    recent = [
        {
            k: h.get(k)
            for k in ("action", "kind", "outcome", "detail")
            if h.get(k) is not None
        }
        for h in history[-12:]
    ]
    prompt = {
        "goal": goal,
        "page": {
            "url": state.get("url"),
            "title": state.get("title"),
            "errors": state.get("errors") or [],
            "required_empty": state.get("required_empty") or [],
            "fields": [
                {k: f[k] for k in ("label", "required", "value", "locked", "error", "actions") if k in f}
                for f in (state.get("fields") or [])
            ],
        },
        "actions": acts,
        "recent_history": recent,
        "instructions": (
            "You are rescuing a stalled browser agent. Pick the single action id "
            "that makes the most progress toward the goal. Rules: prefer actions "
            "that fix entries in errors or fill required_empty fields; do NOT "
            "repeat an action whose recent outcome was unchanged/same_url_changed "
            "or regressed unless the page state clearly changed; do not click "
            "buttons that unlock already-confirmed fields (they undo finished "
            "work); a required select whose options<=1 cannot be chosen yet — "
            "its parent stage or a lookup action must run first; accept dialogs "
            "only if a dialog is pending; do not pick menu/back/save links "
            "while required fields remain empty. "
            "Reply JSON: {\"choice\": \"<action id>\"}."
        ),
    }
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 256,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
        },
    )
    content = result["choices"][0]["message"]["content"]
    try:
        picked = json.loads(content).get("choice")
    except (ValueError, AttributeError):
        return None
    valid = {a["id"] for a in legal}
    if picked not in valid:
        return None
    action = next(a for a in legal if a["id"] == picked)
    candidates = (
        candidate_values(data, action)
        if data and action["kind"] == "fill"
        else []
    )
    probabilities = {i: 0.01 for i in valid}
    probabilities[picked] = 0.9
    return {
        "choice": picked,
        "operation": action["kind"],
        "target": action.get("node"),
        "fill_source": None,
        "fill_candidates": candidates,
        "goal_met": None,
        "progress": None,
        "confidence": 0.9,
        "probabilities": probabilities,
        "operation_probabilities": {},
        "target_probabilities": {},
        "target_confidence": None,
        "raw_answers": {"rescue": {"model": model}},
        "model": model,
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": {"rescue": True},
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [
            {k: h.get(k) for k in ("action", "text")}
            for h in history[-6:]
            # A stale rejection typed nothing — keeping it in context would churn
            # the helper cache key without informing the next value.
            if h.get("outcome") != "stale"
        ],
    }


def field_text(context):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": TEXT_VALUE},
                {
                    "role": "user",
                    "content": json.dumps(context),
                },
            ],
        },
    )
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None
    return value, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
