"""Ask Claude to turn inbox evidence into a broker profile.

This is the one deliberately model-assisted corner of the app, used only when
onboarding a broker that isn't in BROKER_PROFILES yet. The model never reads a
statement — it only classifies sender addresses the inbox scan already found
(statement senders vs marketing noise) and proposes the profile. A person
reviews the proposal before anything is saved, and once saved the broker is
handled by plain code forever, same as the built-ins.

Needs `pip install anthropic` and an Anthropic credential. The key entered in
the Setup tab (stored in config.json) wins; otherwise the SDK falls back to
whatever this machine has — ANTHROPIC_API_KEY (which `.env` can supply) or an
`ant auth login` profile. Without any of those, the scan still works — the
user just ticks the senders themselves.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"


def _settings() -> tuple[str, str]:
    """(api_key, model) as configured; empty key means let the SDK resolve one."""
    try:
        from . import config as cfgmod
        cfg = cfgmod.load()
        return cfg.llm.api_key, (cfg.llm.model or DEFAULT_MODEL)
    except Exception:
        return "", DEFAULT_MODEL


def _client(api_key: str):
    import anthropic
    return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()


def status() -> dict:
    """What the Setup tab shows. Never includes the key itself."""
    api_key, model = _settings()
    try:
        import anthropic  # noqa: F401
        sdk = True
    except ImportError:
        sdk = False
    return {
        "sdk": sdk,
        "key_saved": bool(api_key),
        "env": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "model": model,
        "default_model": DEFAULT_MODEL,
    }


def test_credential() -> tuple[bool, str]:
    """Confirm the configured credential and model actually work.

    Uses the Models API, which validates the key and the model id in one
    request without generating (or paying for) any tokens.
    """
    api_key, model = _settings()
    try:
        import anthropic
    except ImportError:
        return False, "the `anthropic` package isn't installed on the server — `pip install anthropic`"
    try:
        found = _client(api_key).models.retrieve(model)
        return True, f"connected — {found.display_name or found.id} is available on this key"
    except anthropic.AuthenticationError:
        return False, ("the API key was rejected — check it (or create one) at "
                       "console.anthropic.com/settings/keys")
    except anthropic.NotFoundError:
        return False, (f"the key works but there is no model called “{model}” — "
                       f"leave the model blank to use {DEFAULT_MODEL}")
    except anthropic.APIConnectionError:
        return False, "could not reach api.anthropic.com — check the server's network"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

# Structured output keeps the reply machine-readable — no prose to parse.
_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string",
                  "description": "Display name for the broker, e.g. 'Angel One'"},
        "senders": {"type": "array", "items": {"type": "string"},
                    "description": "Only the addresses that mail account documents"},
        "subjects": {"type": "array", "items": {"type": "string"},
                     "description": "Lowercase subject substrings identifying document types"},
        "rationale": {"type": "string",
                      "description": "One or two sentences on why these senders were chosen"},
    },
    "required": ["label", "senders", "subjects", "rationale"],
    "additionalProperties": False,
}

_PROMPT = """\
A portfolio tracker reads Indian stock-broker documents (contract notes and
holdings/transaction statements) from people's email. Only mail from known
sender addresses is ever downloaded, so onboarding the broker "{name}" means
deciding which of its sending addresses carry those documents.

Below are candidate senders found by scanning real inboxes for the last year.
For each: the address, how many messages it sent, how many of those carried a
PDF attachment, and sample subject lines.

{candidates}

Pick ONLY the senders that carry actual account documents — contract notes,
trade confirmations with contract notes attached, and demat/holdings or
transaction statements. Exclude marketing, product announcements, OTPs and
login alerts, fund-transfer receipts, margin/retention statements, and payout
notifications: brokers usually mail each category from its own address, and
subscribing to a noisy address drags hundreds of useless messages into every
sync. A sender with many messages but no PDFs is almost never a document
sender.

For `subjects`, give short lowercase substrings that identify the document
types in this broker's subject lines (e.g. "contract note"). They are used to
classify a downloaded document, not to search mail, so keep them specific.
"""


def propose_profile(broker_name: str, candidates: list[dict]) -> tuple[dict | None, str]:
    """Propose {label, senders, subjects, rationale} for a new broker.

    Returns (profile, note) — profile is None when the model can't run or
    declines, and the note says why so the UI can show it. Never raises: the
    scan results are still useful without the proposal.
    """
    if not candidates:
        return None, "nothing for the model to classify"
    try:
        import anthropic
    except ImportError:
        return None, "Claude assist is off — `pip install anthropic` to enable it"

    api_key, model = _settings()

    lines = []
    for c in candidates:
        subjects = "; ".join(c.get("subjects", [])) or "(no subject recorded)"
        lines.append(f"- {c['sender']} — {c['count']} message(s), "
                     f"{c['pdfs']} with PDF. Subjects: {subjects}")
    prompt = _PROMPT.format(name=broker_name, candidates="\n".join(lines))

    try:
        client = _client(api_key)
        request = dict(
            model=model,
            max_tokens=4000,
            output_config={"format": {"type": "json_schema", "schema": _PROFILE_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        if model in ("claude-opus-5", "claude-fable-5", "claude-mythos-5"):
            # These models run safety classifiers that can decline a request;
            # retry server-side on the recommended fallback model instead of
            # failing the scan. Other models reject the parameter, so it is
            # only sent where it applies.
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request)
        else:
            response = client.messages.create(**request)
        if response.stop_reason == "refusal":
            return None, "the model declined to classify these senders"
        text = next((b.text for b in response.content if b.type == "text"), "")
        profile = json.loads(text)
        return profile, f"proposed by {response.model}"
    except anthropic.AuthenticationError:
        return None, ("Claude assist needs a working credential — add an API key "
                      "under “Claude assist” in this panel, or set ANTHROPIC_API_KEY")
    except Exception as exc:  # network, rate limit, malformed reply — all non-fatal
        log.warning("broker proposal failed: %s", exc)
        return None, f"Claude assist failed ({type(exc).__name__}) — pick senders by hand"
