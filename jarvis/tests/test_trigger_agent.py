"""Phase 13A: the TRIGGER agent. No network: a scripted fake socket feeds it frames."""

import asyncio
import json

import pytest

from jarvis.trigger import TriggerAgent, TriggerConfig, TriggerSignal

A1, A2 = "111", "222"


class FakeWS:
    def __init__(self, frames, hold_open=True):
        self.frames, self.sent, self.hold_open = list(frames), [], hold_open

    async def send(self, msg):
        self.sent.append(msg)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for f in self.frames:
            await asyncio.sleep(0)
            yield f if isinstance(f, str) else json.dumps(f)
        if self.hold_open:
            await asyncio.sleep(3600)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def connector(*sockets):
    it = iter(sockets)
    return lambda url: next(it)


def cfg(**kw):
    base = dict(move_threshold=0.02, cooldown_s=0.0, priority_move_threshold=0.005, priority_cooldown_s=0.0,
                ping_interval_s=9, stale_after_s=999, universe_refresh_s=999, backoff_initial_s=0.01)
    return TriggerConfig(**{**base, **kw})


def pc(token, bid, ask):
    return {"event_type": "price_change", "market": "0xM", "timestamp": "1",
            "price_changes": [{"asset_id": token, "price": str(bid), "size": "1", "side": "BUY",
                               "best_bid": str(bid), "best_ask": str(ask)}]}


def book(token, bid, ask):
    return {"event_type": "book", "market": "0xM", "asset_id": token,
            "bids": [{"price": str(bid - 0.01), "size": "5"}, {"price": str(bid), "size": "5"}],
            "asks": [{"price": str(ask + 0.01), "size": "5"}, {"price": str(ask), "size": "5"}]}


class Rig:
    def __init__(self, frames=(), sockets=None, config=None, priority=(), universe=None, delay=0.0, **hooks):
        self.signals: list[TriggerSignal] = []
        self.delay = delay

        async def handler(sig):
            self.signals.append(sig)
            if self.delay:
                await asyncio.sleep(self.delay)

        async def provider():
            return universe() if callable(universe) else (universe or [A1, A2])
        self.ws = sockets or [FakeWS(frames)]
        self.agent = TriggerAgent(handler, provider, config or cfg(), priority=lambda t: t in priority,
                                  connect=connector(*self.ws), **hooks)

    async def run(self, seconds=0.2):
        t = asyncio.create_task(self.agent.run())
        await asyncio.sleep(seconds)
        self.agent.stop()
        await asyncio.wait_for(t, 2)


def go(coro):
    asyncio.run(coro)


def test_subscribe_frame_and_baseline_is_not_a_signal():
    async def t():
        r = Rig([[book(A1, 0.50, 0.52), book(A2, 0.30, 0.32)]])     # the first frame is an array
        await r.run()
        sub = json.loads(r.ws[0].sent[0])
        assert sub == {"type": "market", "assets_ids": [A1, A2], "custom_feature_enabled": True}
        assert r.signals == []
    go(t())


def test_move_over_threshold_signals_with_delta():
    async def t():
        r = Rig([pc(A1, .50, .52), pc(A1, .51, .53), pc(A1, .53, .55)])
        await r.run()
        assert len(r.signals) == 1 and r.signals[0].mid == 0.54 and round(r.signals[0].delta, 4) == 0.03
        assert r.signals[0].priority is False and r.signals[0].reason == "price_move"
    go(t())


def test_priority_token_signals_on_baseline_and_small_moves():
    async def t():
        r = Rig([pc(A1, .50, .52), pc(A1, .495, .515)], priority={A1})     # -0.005
        await r.run()
        assert [s.reason for s in r.signals] == ["baseline", "price_move"]
        assert all(s.priority for s in r.signals)
    go(t())


def test_wide_spread_ignored_for_watchlist_but_not_for_held_tokens():
    async def t():
        r = Rig([pc(A1, .50, .52), pc(A1, .40, .70), pc(A2, .50, .52), pc(A2, .40, .70)], priority={A2})
        await r.run()
        assert [s.token_id for s in r.signals if s.reason == "price_move"] == [A2]
    go(t())


def test_cooldown_blocks_repeats():
    async def t():
        r = Rig([pc(A1, .50, .52), pc(A1, .60, .62)], config=cfg(cooldown_s=60))
        await r.run()
        assert r.signals == []
    go(t())


def test_unwatched_token_and_crossed_book_ignored():
    async def t():
        quotes = []
        r = Rig([pc("999", .5, .52), pc("999", .7, .72), pc(A1, .60, .55)], on_quote=lambda *a: quotes.append(a))
        await r.run()
        assert r.signals == [] and quotes == []
    go(t())


def test_quote_hook_gets_every_valid_quote():
    async def t():
        quotes = []
        r = Rig([pc(A1, .50, .52), pc(A1, .51, .53)], on_quote=lambda *a: quotes.append(a))
        await r.run()
        assert [(q[0], q[2], q[3]) for q in quotes] == [(A1, .50, .52), (A1, .51, .53)]
    go(t())


def test_coalesces_to_newest_price_while_handler_busy():
    async def t():
        frames = [pc(A2, .30, .32), pc(A2, .35, .37), pc(A1, .50, .52)]
        frames += [pc(A1, .50 + .03 * i, .52 + .03 * i) for i in range(1, 8)]
        r = Rig(frames, delay=0.1)
        await r.run(0.6)
        a1 = [s for s in r.signals if s.token_id == A1]
        assert len(a1) == 1 and a1[0].mid == round((.71 + .73) / 2, 4)
        assert r.agent.stats.coalesced >= 5
    go(t())


def test_handler_error_does_not_kill_the_agent():
    async def t():
        r = Rig([pc(A1, .50, .52), pc(A1, .60, .62), pc(A1, .70, .72)])
        calls = []

        async def bad(sig):
            calls.append(sig)
            raise RuntimeError("boom")
        r.agent.on_signal = bad
        await r.run()
        assert len(calls) >= 1 and r.agent.stats.handler_errors == len(calls)
    go(t())


def test_reconnects_after_server_close_and_after_silence():
    async def t():
        r = Rig(sockets=[FakeWS([pc(A1, .5, .52)], hold_open=False), FakeWS([pc(A1, .5, .52), pc(A1, .6, .62)])])
        await r.run(0.4)
        assert r.agent.stats.connects == 2 and len(r.signals) == 1

        r = Rig(sockets=[FakeWS([]), FakeWS([])], config=cfg(stale_after_s=0.1, ping_interval_s=0.05))
        await r.run(0.6)
        assert r.agent.stats.connects == 2
    go(t())


def test_pong_and_garbage_are_ignored():
    async def t():
        r = Rig(["PONG", "not json", pc(A1, .5, .52)])
        await r.run()
        assert r.agent.stats.frames == 3
    go(t())


def test_universe_refresh_subscribes_and_unsubscribes_the_difference():
    async def t():
        sets = [[A1, A2], [A2, "333"]]
        r = Rig([], universe=lambda: sets.pop(0) if len(sets) > 1 else sets[0], config=cfg(universe_refresh_s=0.05))
        await r.run(0.15)
        ops = [json.loads(m) for m in r.ws[0].sent[1:]]
        assert {"assets_ids": ["333"], "operation": "subscribe"} in ops
        assert {"assets_ids": [A1], "operation": "unsubscribe"} in ops
    go(t())


def test_market_resolved_reaches_the_hook():
    async def t():
        seen = []

        async def hook(ev):
            seen.append(ev["winning_asset_id"])
        ev = {"event_type": "market_resolved", "id": "1", "market": "0xM", "assets_ids": [A1, A2],
              "winning_asset_id": A1, "winning_outcome": "Yes", "timestamp": "1"}
        r = Rig([ev], on_resolved=hook)
        await r.run()
        assert seen == [A1]
    go(t())


def test_config_rejects_unsafe_values():
    for bad in (dict(ws_url="ws://insecure"), dict(priority_move_threshold=0.5, move_threshold=0.02),
                dict(ping_interval_s=30), dict(max_spread=0)):
        with pytest.raises(ValueError):
            TriggerConfig(**bad)
