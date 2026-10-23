"""Where an alert goes, and how often.

Two channels, one rule: an alert is a *change*, not a heartbeat.  A daemon
that posts every hour is a daemon the player mutes.

* a Discord webhook (``DFA_DISCORD_WEBHOOK`` in the environment, never in
  this file and never in the log), with one embed per alert;
* a Windows toast through ``System.Windows.Forms.NotifyIcon``, which needs
  no module and no administrator.

Every channel is best-effort: a dead webhook or a missing PowerShell must
not stop the pipeline, so failures are returned and logged, never raised.

Rate limiting is per key (``realm:item:advice``) with a cooldown, kept in
the daemon's state file.  The same undercut noticed every hour is one
message per cooldown, and a worsening one is a new message because the
key contains the advice.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: the project already keeps a Discord webhook for its reports under the
#: short name; the DFA_ name is the explicit override
WEBHOOK_ENVS = ("DFA_DISCORD_WEBHOOK", "DISCORD_WEBHOOK")
WEBHOOK_ENV = WEBHOOK_ENVS[0]
DEFAULT_COOLDOWN_S = 6 * 3600

COLORS = {"info": 0x3498DB, "warn": 0xE67E22, "crit": 0xE74C3C}


@dataclass
class Alert:
    key: str
    title: str
    lines: List[str] = field(default_factory=list)
    level: str = "info"

    def as_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "title": self.title,
                "lines": list(self.lines), "level": self.level}


def webhook_url(environ: Optional[Dict[str, str]] = None) -> str:
    """The configured webhook, or an empty string.  Never logged.

    ``DFA_DISCORD_WEBHOOK`` wins, ``DISCORD_WEBHOOK`` is the name the repo
    already uses in ``.env``.  A missing value is not an error: the desktop
    toast still works.
    """
    env = environ if environ is not None else os.environ
    for name in WEBHOOK_ENVS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def redact(url: str) -> str:
    """A webhook URL safe to print: the token is the URL."""
    if not url:
        return ""
    return url.split("/webhooks/", 1)[0] + "/webhooks/<hidden>"


def discord_payload(alert: Alert) -> Dict[str, Any]:
    """The webhook body.  One embed: title, lines, colour by level."""
    description = "\n".join(alert.lines)[:3800]
    return {
        "username": "DFA Sentinel",
        "embeds": [{
            "title": alert.title[:240],
            "description": description if description else "\u200b",
            "color": COLORS.get(alert.level, COLORS["info"]),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "DFA pricing intelligence"},
        }],
    }


def post_discord(url: str, payload: Dict[str, Any],
                 opener: Callable[..., Any] = urllib.request.urlopen,
                 timeout: float = 10.0) -> Dict[str, Any]:
    """POST one payload.  Returns ``{ok, status, error}`` and never raises."""
    if not url:
        return {"ok": False, "status": 0, "error": "no webhook configured"}
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        # Cloudflare in front of Discord rejects the default Python urllib
        # user agent with a 403 that looks like an invalid webhook
        headers={"Content-Type": "application/json",
                 "User-Agent": "DFA-Sentinel/1.0"},
        method="POST")
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 0) or 0
            return {"ok": 200 <= int(status) < 300, "status": int(status),
                    "error": ""}
    except urllib.error.HTTPError as exc:  # a 404 webhook is a real answer
        return {"ok": False, "status": int(exc.code), "error": f"HTTP {exc.code}"}
    except Exception as exc:  # network, DNS, TLS, timeout
        return {"ok": False, "status": 0, "error": str(exc)[:200]}


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def desktop_notify(title: str, body: str,
                   runner: Optional[Callable[[List[str]], Any]] = None) -> Dict[str, Any]:
    """A Windows balloon notification; silent failure elsewhere."""
    if runner is None:
        if os.name != "nt":
            return {"ok": False, "error": "not Windows"}
        def runner(cmd):  # noqa: E306 - tiny adapter
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(8000, {_ps_quote(title)}, {_ps_quote(body)},"
        "[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Milliseconds 9000; $n.Dispose()"
    )
    try:
        runner(["powershell", "-NoProfile", "-NonInteractive",
                "-WindowStyle", "Hidden", "-Command", script])
        return {"ok": True, "error": ""}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def allow(state: Dict[str, Any], key: str, now: int,
          cooldown_s: int = DEFAULT_COOLDOWN_S) -> bool:
    """Rate limit by key, using a plain dict the caller persists."""
    sent = state.setdefault("alerted", {})
    last = int(sent.get(key) or 0)
    if last and now - last < cooldown_s:
        return False
    sent[key] = int(now)
    return True


def deliver(alerts: Iterable[Alert], state: Dict[str, Any], now: int,
            url: str = "", cooldown_s: int = DEFAULT_COOLDOWN_S,
            notify: bool = True, dry_run: bool = False) -> List[Dict[str, Any]]:
    """Rate-limits, posts, toasts.  Returns what happened, per alert."""
    results = []
    for alert in alerts:
        if not allow(state, alert.key, now, cooldown_s):
            results.append({"key": alert.key, "skipped": "cooldown"})
            continue
        if dry_run:
            results.append({"key": alert.key, "skipped": "dry-run"})
            continue
        posted = post_discord(url, discord_payload(alert)) if url else {
            "ok": False, "status": 0, "error": "no webhook configured"}
        toasted = desktop_notify(alert.title, " | ".join(alert.lines[:3])) \
            if notify else {"ok": False, "error": "disabled"}
        if not posted["ok"]:
            logger.warning("webhook %s failed: %s", alert.key, posted["error"])
        # A cooldown records that a human was told.  When no channel
        # delivered it -- a dead webhook and no toast -- the alert is
        # retried on the next tick instead of being silenced for six hours.
        retry = not posted["ok"] and not toasted["ok"]
        if retry:
            state.get("alerted", {}).pop(alert.key, None)
        results.append({"key": alert.key, "webhook": posted["ok"],
                        "toast": toasted["ok"], "error": posted["error"],
                        "retry": retry})
    return results


def undercut_alerts(advice: Dict[str, Any], limit: int = 5) -> List[Alert]:
    """One digest of actionable listings, and a separate review digest.

    A review alert is not a weaker cancel instruction: it says the evidence
    is insufficient to compare the asks, so the player has to look at the
    item or run a verified scan.  Keeping it out of the actionable digest is
    what stops a price-only or variant-ambiguous row from becoming a relist.
    """
    all_rows = list(advice.get("advice") or [])
    rows = [row for row in all_rows
            if row.get("advice") in ("cancel_and_relist", "let_expire")]
    review = [row for row in all_rows
              if row.get("review_required")
              and row.get("advice") not in ("cancel_and_relist", "let_expire")]
    realm = advice.get("realm") or "?"
    counts = advice.get("counts") or {}
    if not rows:
        if not review:
            return []
        lines = []
        for row in review[:limit]:
            price = (row.get("own_unit_price_copper") or 0) / 10000
            low = (row.get("lowest_unit_price_copper") or 0) / 10000
            name = (row.get("item_name") or
                    f"item {row.get('item_id') or '?'}")
            status = row.get("reference_status") or "unverified"
            lines.append(f"{name}: {price:.0f}g vs {low:.0f}g; {status}"
                         " reference, no automatic relist")
        lines.append("relist=0 expire=0 review=" + str(len(review))
                     + "; open the Auction House for a verified scan")
        return [Alert(key=f"undercut-review:{realm}",
                      title=f"{realm}: {len(review)} listing(s) need review",
                      lines=lines, level="info")]
    lines = []
    for row in rows[:limit]:
        price = (row.get("own_unit_price_copper") or 0) / 10000
        low = (row.get("lowest_unit_price_copper") or 0) / 10000
        where = row.get("lowest_seller") or "a listing"
        name = (row.get("item_name") or
                f"item {row.get('item_id') or '?'}")
        # The gap is (own - lowest) / own: the competitor is that fraction
        # below *my* ask.  Saying only "99% below" made it look as if my
        # listing were below the reference.
        gap = row.get("gap_pct") or 0
        lines.append(f"{name}: {price:.0f}g vs {low:.0f}g"
                     f" (cheapest is {gap * 100:.0f}%"
                     f" below your ask, {where})"
                     f" -> {row.get('advice')}")
    lines.append("relist=" + str(counts.get("cancel_and_relist", 0))
                 + " expire=" + str(counts.get("let_expire", 0))
                 + " review=" + str(len(review)))
    return [Alert(key=f"undercut:{realm}:{rows[0].get('item_name')}",
                  title=f"{realm}: {len(rows)} listing(s) need attention",
                  lines=lines, level="warn")]


def reset_alerts(resets: List[Dict[str, Any]], names: Dict[int, str],
                 realm: str, limit: int = 5) -> List[Alert]:
    """A buy-out and repost changes what every ask is worth: say so."""
    if not resets:
        return []
    lines = []
    for row in resets[:limit]:
        lines.append(f"{names.get(row['item_id'], 'item ' + str(row['item_id']))}:"
                     f" cheapest {row['previous_cheapest'] / 10000:.0f}g ->"
                     f" {row['current_cheapest'] / 10000:.0f}g"
                     f" ({row['jump_pct'] * 100:.0f}%), supply"
                     f" -{row['supply_drop_pct'] * 100:.0f}%")
    return [Alert(key=f"reset:{realm}:{resets[0]['item_id']}",
                  title=f"{realm}: possible market reset ({len(resets)} item(s))",
                  lines=lines, level="crit")]


def inventory_alerts(summary: Dict[str, Any], realm: str) -> List[Alert]:
    """Unexplained stock movements are a data problem, not a sale."""
    if not summary:
        return []
    decrease = int(summary.get("unexplained_decrease_units") or 0)
    if decrease < 50:
        return []
    return [Alert(key=f"inventory:{realm}",
                  title=f"{realm}: {decrease} unit(s) disappeared unexplained",
                  lines=[f"snapshot {summary.get('snapshot_id')}",
                         "not a sale: no confirmed event covers it",
                         "open the bank/AH with the addon before trusting stock"],
                  level="warn")]


def error_alert(kind: str, detail: str) -> Alert:
    return Alert(key=f"error:{kind}", title=f"DFA Sentinel: {kind} failed",
                 lines=[str(detail)[:500]], level="crit")
