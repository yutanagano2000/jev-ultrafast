"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import re
import time
from pathlib import Path

from .browser import Browser, StalePage
from .model import action_space, choose, field_context, field_text, rescue_decision
from .questions import MAX_STEPS


def _to_fullwidth(text):
    # A field whose label declares 全角 rejects half-width input at submit time.
    # Convert ASCII range (space->U+3000, '-'->U+FF0D included) mechanically.
    return "".join(
        chr(ord(c) + 0xFEE0) if 0x21 <= ord(c) <= 0x7E else ("　" if c == " " else c)
        for c in text
    )


def _action_key(h):
    # Node ids can churn across partial re-renders; the whitespace-normalized
    # label is the stablest identity for repeat/streak detection.
    return re.sub(r"\s+", "", h["action"])


def resolve_value(raw, action):
    """Map a data value onto the observed field. Split fields carry an (n/m)
    marker in their label; each box receives the n-th '-'-separated part."""
    label = action.get("label") or ""
    match = re.search(r"\((\d+)/(\d+)\)\s*$", label)
    if match:
        parts = re.split(r"[-‐−―ー]", raw)
        index, total = int(match.group(1)), int(match.group(2))
        if len(parts) == total:
            raw = parts[index - 1]
    if "全角" in label:
        # Full-width fields commonly also reject lowercase latin; uppercasing
        # is the safest generic form.
        raw = _to_fullwidth(raw.upper())
    return raw


class Agent:
    @staticmethod
    def _wait_stable(browser, timeout=4.0):
        """Wait until observed DOM mutations stop for ~0.5s (or timeout).

        Installs a MutationObserver-backed counter on first use; reads it via
        Runtime.evaluate. A frozen renderer (native dialog) or navigation makes
        evaluate raise — that simply ends the wait early."""
        install = (
            "(()=>{if(!window.__jevMut){let n=0;"
            "new MutationObserver(()=>n++).observe(document.documentElement,"
            "{childList:true,subtree:true,attributes:true,characterData:true});"
            "window.__jevMut=()=>n;}return window.__jevMut();})()"
        )
        try:
            last = browser.evaluate(install)
        except Exception:
            time.sleep(0.3)
            return
        stable = 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.25)
            try:
                n = browser.evaluate("window.__jevMut ? window.__jevMut() : -1")
            except Exception:
                return
            if n == last:
                stable += 1
                if stable >= 2:
                    return
            else:
                last, stable = n, 0

    def __init__(self, url, goals, *, record_dir=None, screenshots=False, data=None):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
        self._rescue_last = -10
        self._rescue_count = 0
        self.data = dict(data or {})
        self.browser = Browser(url)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            if page.get("screenshot"):
                (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if len(state["decisions"]) >= MAX_STEPS * 4:
                raise ValueError("Reached the demo's model-call budget")
            # Stall escalation: five consecutive weak outcomes means the small
            # model is looping; a stronger model picks one action from the
            # same observed space. Capped and cooled down so it stays rare.
            weak = {"unchanged", "same_url_changed", "no_value", "dialog_opened", "dialog_answered", "stale"}
            stalled = (
                len(state["history"]) >= 5
                and all(h.get("outcome") in weak for h in state["history"][-5:])
            )
            if (
                stalled
                and state["history"][-1].get("step", 0) - self._rescue_last > 5
                and self._rescue_count < 15
            ):
                try:
                    state["decision"] = rescue_decision(
                        state["page"], state["goal"], state["history"], data=self.data
                    )
                    if state["decision"]:
                        self._rescue_count += 1
                        self._rescue_last = state["history"][-1].get("step", 0)
                except Exception:
                    state["decision"] = None
            if state["decision"] is None:
                try:
                    state["decision"] = choose(
                        state["page"], state["goal"], state["history"], data=self.data
                    )
                except ValueError:
                    # A malformed model response is read-only; one retry is safe
                    # (mutations are never retried — only decisions).
                    state["decision"] = choose(
                        state["page"], state["goal"], state["history"], data=self.data
                    )
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= MAX_STEPS:
                state["status"] = "blocked"
                raise ValueError(f"Stopped at the {MAX_STEPS}-action demo budget")
            text, helper = None, None
            if action["kind"] == "fill":
                source = decision.get("fill_source")
                candidates = decision.get("fill_candidates") or []
                if source:
                    # Jev picked a provided value; code injects it (with split
                    # handling for (n/m) segmented fields).
                    text = resolve_value(str(source), action)
                    helper = {"model": "data", "latency_ms": 0}
                elif self.data and action.get("required") and candidates:
                    # Benchmark rule: a required field must hold SOME format-valid
                    # value; when the model declines, inject the first candidate.
                    # Record it as fill_source so error-retry rotation retires it.
                    decision["fill_source"] = str(candidates[0])
                    text = resolve_value(decision["fill_source"], action)
                    helper = {"model": "data:first", "latency_ms": 0}
                elif self.data and action.get("required") and not decision.get("fill_source"):
                    # Last resort for a required field no profile value fits:
                    # a neutral placeholder keeps benchmark runs moving.
                    text = "テスト" if "全角" in (action.get("label") or "") else "test"
                    helper = {"model": "placeholder", "latency_ms": 0}
                elif self.data:
                    # A data profile exists and the model answered "none": the
                    # field stays empty by policy. No text-helper round-trip.
                    text, helper = None, {"model": "none", "latency_ms": 0}
                else:
                    if not state["browser"].fresh(page):
                        raise StalePage("Page changed before text generation. Choose again.")
                    context = field_context(state["goal"], action, page, state["history"])
                    if self.pending_text and self.pending_text[0] == context:
                        _, text, helper = self.pending_text
                    else:
                        try:
                            text, helper = field_text(context)
                        except ValueError:
                            text, helper = None, {"model": "none", "latency_ms": 0}
                        if text:
                            self.pending_text = (context, text, helper)
                            state["text_calls"].append({**helper, "field": action["label"], "value": text})
                if text is None:
                    # No value available for this field: record the miss so the
                    # next decision sees it, without mutating the page.
                    state["history"].append(
                        {
                            "step": len(state["history"]) + 1,
                            "action": action["label"],
                            "node": action.get("node"),
                            "kind": action["kind"],
                            "choice": selected,
                            "probability": decision["probabilities"][selected],
                            "confidence": decision["confidence"],
                            "latency_ms": decision["latency_ms"],
                            "text": None,
                            "text_helper": helper["model"] if helper else None,
                            "text_latency_ms": helper["latency_ms"] if helper else 0,
                            "operation": decision["operation"],
                            "target": decision["target"],
                            "fill_source": decision.get("fill_source"),
                            "goal_met": decision.get("goal_met"),
                            "progress": decision.get("progress"),
                            "outcome": "no_value",
                            "page_changed": False,
                            "url": page["url"],
                            "usage": decision["usage"],
                            "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                            "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                        }
                    )
                    repeated = state["history"][-5:]
                    if (
                        len(repeated) == 5
                        and len({_action_key(h) for h in repeated}) == 1
                    ):
                        state["status"] = "blocked"
                    else:
                        state["status"] = "ready"
                    return self.snapshot()
            # Browser.act checks freshness immediately before input, including after text generation.
            try:
                result = state["browser"].act(action, page, text=text)
            except StalePage:
                # A stale rejection is still an outcome for this action — without
                # recording it, suppression never sees the failure and the same
                # untouchable target gets chosen forever (one per model call).
                state["history"].append(
                    {
                        "step": len(state["history"]) + 1,
                        "action": action["label"],
                        "node": action.get("node"),
                        "kind": action["kind"],
                        "choice": selected,
                        "probability": decision["probabilities"][selected],
                        "confidence": decision["confidence"],
                        "latency_ms": decision["latency_ms"],
                        "text": None,
                        "text_helper": None,
                        "text_latency_ms": 0,
                        "operation": decision["operation"],
                        "target": decision["target"],
                        "fill_source": None,
                        "goal_met": decision.get("goal_met"),
                        "progress": decision.get("progress"),
                        "outcome": "stale",
                        "page_changed": None,
                        "url": page["url"],
                        "usage": decision["usage"],
                        "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                        "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                        "fp": page["fingerprint"],
                        "errsig": "|".join(
                            sorted(
                                (page.get("errors") or [])
                                + (page.get("required_empty") or [])
                            )
                        ),
                    }
                )
                repeated = state["history"][-5:]
                if (
                    len(repeated) == 5
                    and len({_action_key(h) for h in repeated}) == 1
                    and repeated[0]["kind"] in {"click", "fill", "select", "reveal"}
                ):
                    state["status"] = "blocked"
                raise
            if action["kind"] in {"select", "click"}:
                # Selects and buttons often fire onchange/postbacks that
                # re-render dependent controls; wait until the DOM stops
                # mutating so the next snapshot sees the settled state.
                self._wait_stable(state["browser"])
            value_after = (result or {}).get("value_after")
            outcome = None
            if action["kind"] == "fill" and value_after is not None:
                if value_after != text:
                    outcome = f"value_differs:{value_after}"
                elif str(action.get("value") or "") == str(text):
                    outcome = "unchanged"  # already held this value; typing was a no-op
                else:
                    outcome = "value_set"
            elif action["kind"] == "select" and value_after is not None:
                outcome = "value_set"
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "node": action.get("node"),
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "fill_source": decision.get("fill_source"),
                    "goal_met": decision.get("goal_met"),
                    "progress": decision.get("progress"),
                    "outcome": outcome,
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            changed = state["page"]["fingerprint"] != page["fingerprint"]
            outcome = state["history"][-1]["outcome"]
            if outcome is None:
                if action["kind"] == "dialog" or state["page"]["title"] == "native dialog":
                    # Opening or answering a dialog is not progress by itself;
                    # count it neutrally so dialog flows cannot masquerade as
                    # navigation (and cannot loop on that fake progress).
                    outcome = "dialog_opened" if state["page"]["title"] == "native dialog" else "dialog_answered"
                elif state["page"]["url"] != page["url"]:
                    outcome = "navigated"
                elif changed:
                    outcome = "same_url_changed"
                    if action["kind"] == "click" and len(
                        state["page"].get("locked") or []
                    ) < len(page.get("locked") or []):
                        # The click reduced confirmed (disabled+valued) controls:
                        # it reopened finished work — a regression, not progress.
                        outcome = "regressed"
                else:
                    outcome = "unchanged"
            state["history"][-1].update(
                page_changed=changed,
                outcome=outcome,
                url=state["page"]["url"],
                fp=state["page"]["fingerprint"],
                # What the page said back: the alert a click raised, or the
                # dialog a dialog action just answered. Without the message the
                # model sees "a dialog happened" but not what it asked for —
                # the text usually names the field to fix next.
                detail=(
                    (state["page"].get("dialog") or {}).get("message")
                    or (page.get("dialog") or {}).get("message")
                ),
                errsig="|".join(
                    sorted(
                        (state["page"].get("errors") or [])
                        + (state["page"].get("required_empty") or [])
                    )
                ),
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"] and state["page"].get("screenshot"):
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            # Parking (in choose) already hides a thrice-failed label for one
            # decision; reaching five identical weak outcomes means the detour
            # failed too — only then stop the run.
            repeated = state["history"][-5:]
            same_target = (
                len(repeated) == 5
                and len({_action_key(h) for h in repeated}) == 1
                and repeated[0]["kind"] in {"click", "fill", "select"}
            )
            state["status"] = (
                "blocked"
                if same_target
                or (
                    len(repeated) == 3
                    and all(
                        h["page_changed"] is False
                        and h["kind"] in {"click", "dialog"}
                        and h.get("outcome") == "unchanged"
                        for h in repeated
                    )
                )
                else "ready"
            )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
