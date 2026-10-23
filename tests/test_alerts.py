"""Alerts: one message per change, and a dead channel is not an outage."""
import urllib.error

from woweconomy.intel import alerts


class Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_the_repo_webhook_name_is_read_and_the_dfa_name_wins():
    assert alerts.webhook_url({"DISCORD_WEBHOOK": " short "}) == "short"
    assert alerts.webhook_url({
        "DISCORD_WEBHOOK": "old", "DFA_DISCORD_WEBHOOK": "new"}) == "new"
    assert alerts.webhook_url({}) == ""


def test_a_webhook_url_is_never_printed():
    url = "https://discord.com/api/webhooks/123/abcSECRET"
    assert "abcSECRET" not in alerts.redact(url)
    assert alerts.redact(url).startswith("https://discord.com")


def test_the_payload_carries_the_title_lines_and_level_colour():
    alert = alerts.Alert(key="k", title="Dragonmaw: 2 listings",
                         lines=["Thing: 10g vs 8g -> cancel_and_relist"],
                         level="warn")
    payload = alerts.discord_payload(alert)

    embed = payload["embeds"][0]
    assert embed["title"] == "Dragonmaw: 2 listings"
    assert "cancel_and_relist" in embed["description"]
    assert embed["color"] == alerts.COLORS["warn"]


def test_a_successful_post_reports_ok():
    seen = {}

    def opener(request, timeout=10):
        seen["url"] = request.full_url
        seen["body"] = request.data
        seen["agent"] = request.get_header("User-agent")
        return Response()

    result = alerts.post_discord("https://example.test/hook", {"a": 1}, opener=opener)

    assert result == {"ok": True, "status": 204, "error": ""}
    assert b'"a": 1' in seen["body"]
    # Discord's Cloudflare rejects the default Python agent with a 403
    assert seen["agent"] == "DFA-Sentinel/1.0"


def test_a_dead_webhook_is_reported_not_raised():
    def opener(request, timeout=10):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    result = alerts.post_discord("https://example.test/hook", {}, opener=opener)

    assert result["ok"] is False
    assert result["status"] == 404


def test_no_webhook_is_not_an_error():
    result = alerts.post_discord("", {})

    assert result["ok"] is False
    assert "no webhook" in result["error"]


def test_the_same_key_waits_for_the_cooldown_and_a_new_key_does_not():
    state = {}
    assert alerts.allow(state, "undercut:Dragonmaw:Thing", now=1_000)
    assert not alerts.allow(state, "undercut:Dragonmaw:Thing", now=1_100)
    assert alerts.allow(state, "undercut:Dragonmaw:Other", now=1_100)
    assert alerts.allow(state, "undercut:Dragonmaw:Thing", now=1_000 + 6 * 3600)


def test_a_dry_run_consumes_the_cooldown_without_posting():
    state = {}
    results = alerts.deliver(
        [alerts.Alert(key="k", title="t", lines=["l"])], state, now=10,
        url="https://example.test/hook", dry_run=True)

    assert results == [{"key": "k", "skipped": "dry-run"}]
    assert not alerts.allow(state, "k", now=11)


def test_an_undelivered_alert_is_retried_instead_of_silenced(monkeypatch):
    monkeypatch.setattr(alerts, "post_discord",
                        lambda url, payload: {"ok": False, "status": 0,
                                              "error": "down"})
    state = {}
    alert = alerts.Alert(key="k", title="t", lines=["l"])

    first = alerts.deliver([alert], state, now=100,
                           url="https://example.test/hook", notify=False)
    second = alerts.deliver([alert], state, now=200,
                            url="https://example.test/hook", notify=False)

    assert first[0]["webhook"] is False and first[0]["retry"] is True
    # the next tick tries again: no six-hour silence for a message nobody saw
    assert "skipped" not in second[0]
    assert second[0]["retry"] is True


def test_a_delivered_alert_keeps_its_cooldown(monkeypatch):
    monkeypatch.setattr(alerts, "post_discord",
                        lambda url, payload: {"ok": True, "status": 204,
                                              "error": ""})
    state = {}
    alert = alerts.Alert(key="k", title="t", lines=["l"])

    alerts.deliver([alert], state, now=100,
                   url="https://example.test/hook", notify=False)
    again = alerts.deliver([alert], state, now=200,
                           url="https://example.test/hook", notify=False)

    assert again == [{"key": "k", "skipped": "cooldown"}]


def test_the_toast_escapes_quotes_and_is_hidden():
    commands = []

    def runner(cmd):
        commands.append(cmd)

    result = alerts.desktop_notify("It's a 'test'", "line", runner=runner)

    assert result["ok"] is True
    command = commands[0][-1]
    assert "It''s a ''test''" in command
    assert "-WindowStyle" in commands[0]


def test_an_undercut_digest_names_the_item_the_gap_and_the_action():
    advice = {
        "realm": "Dragonmaw",
        "counts": {"cancel_and_relist": 1, "let_expire": 0, "hold": 4},
        "advice": [{
            "item_name": "Thing", "own_unit_price_copper": 10_000,
            "lowest_unit_price_copper": 8_000, "gap_pct": 0.2,
            "lowest_seller": "Under-Stormscale", "advice": "cancel_and_relist",
        }],
    }

    digest = alerts.undercut_alerts(advice)

    assert len(digest) == 1
    assert "Thing" in digest[0].lines[0]
    assert "Under-Stormscale" in digest[0].lines[0]
    assert "cheapest is 20% below your ask" in digest[0].lines[0]
    body = "\n".join(digest[0].lines)
    assert "relist=1" in body
    # The reader is usually away from the game, so the digest says what to
    # do rather than leaving a count to be interpreted.
    assert "action:" in body
    assert "cancel" in body.lower()


def test_an_alert_always_carries_the_item_id_and_the_name():
    """A name alone is ambiguous; an id alone is unreadable.  Send both."""
    advice = {
        "realm": "Dragonmaw",
        "counts": {"cancel_and_relist": 1},
        "advice": [{
            "item_id": 12345, "item_name": "Thing",
            "own_unit_price_copper": 10_000,
            "lowest_unit_price_copper": 8_000, "gap_pct": 0.2,
            "advice": "cancel_and_relist",
        }],
    }

    line = alerts.undercut_alerts(advice)[0].lines[0]

    assert "Thing" in line
    assert "item 12345" in line


def test_an_item_with_no_name_still_carries_its_id():
    advice = {
        "realm": "Dragonmaw",
        "counts": {"cancel_and_relist": 1},
        "advice": [{
            "item_id": 999, "own_unit_price_copper": 10_000,
            "lowest_unit_price_copper": 8_000, "gap_pct": 0.2,
            "advice": "cancel_and_relist",
        }],
    }

    line = alerts.undercut_alerts(advice)[0].lines[0]

    assert "item 999" in line
    assert "None" not in line


def test_an_item_with_neither_does_not_print_a_question_mark():
    """An unknown item is said plainly, not disguised as an id of '?'."""
    advice = {
        "realm": "Dragonmaw",
        "counts": {"cancel_and_relist": 1},
        "advice": [{
            "own_unit_price_copper": 10_000,
            "lowest_unit_price_copper": 8_000, "gap_pct": 0.2,
            "advice": "cancel_and_relist",
        }],
    }

    line = alerts.undercut_alerts(advice)[0].lines[0]

    assert "unknown item" in line
    assert "item ?" not in line


def test_a_review_only_result_does_not_become_a_cancel_alert():
    advice = {
        "realm": "Dragonmaw",
        "counts": {"cancel_and_relist": 0, "let_expire": 0, "hold": 0,
                   "watch": 1},
        "advice": [{
            "item_name": "Thing", "own_unit_price_copper": 10_000,
            "lowest_unit_price_copper": 8_000, "gap_pct": 0.2,
            "lowest_seller": "", "advice": "watch",
            "reference_status": "variant-ambiguous",
            "review_required": True,
        }],
    }

    digest = alerts.undercut_alerts(advice)

    assert len(digest) == 1
    assert "need review" in digest[0].title
    assert "no automatic relist" in digest[0].lines[0]
    assert "relist=0" in digest[0].lines[-1]
    assert "cancel_and_relist" not in "\n".join(digest[0].lines)
