"""
FRIDAY market segments (Phase 8B.2; scope setting 8C.2).

Calibration bias is NOT universal: a 45c price on tonight's baseball game,
on one candidate in a 20-name field, and on a long-dated "by December"
question are different animals. The first 8B run on jarvis020 proved it:
one pooled bias for the 40-50c band produced identical "+6.17c" edges on
four different MLB games. So calibration is measured, and applied, per
segment — never borrowed across segments.

  sports  a game/match/series or a line on one ("A vs. B", "Spread:", "O/U",
          "win on 2026-09-27", "(BO5)", "Game 3 Winner", map/set markets).
          Its end date is a resolution buffer, not the event, so the 24h
          calibration horizon is used.
          8C adds fight and prop wording seen on jarvis020: "win by KO or
          TKO", "by submission/decision", "go the distance", rounds,
          "Game Handicap:", halves/quarters/periods/innings, moneyline,
          match winner, UFC/MMA cards. A matchmaking question ("Will X
          fight Y next?") is NOT a game — it resolves over weeks — so it
          stays binary.
          8C.2: a bare "Over/Under" is NOT sports — jarvis020 showed
          "Berlin State Election: SPD Over/Under 13%?". It counts only with
          a non-percentage line ("Over/Under 24.5"); "Total Rounds" added.
  field   one outcome of a multi-outcome event (Polymarket neg-risk: "Will X
          win the Ballon d'Or?", "next Prime Minister", candidate markets).
  binary  every other yes/no event question.

9B.2 — "micro": recurring ultra-short markets ("Bitcoin Up or Down - October 2,
12:35PM-12:40PM ET"). Each is created about a day ahead, sits at 50c, and is
decided inside a five-minute window. Polymarket lists over a thousand a day.
FRIDAY scans every 15 minutes, so it cannot act inside that window, and
their flat 50c history would swamp the calibration tables. They are their
own segment and are out of scope by default, like sports.

8C.2 — SCOPE. The owner's decision (2026-10-02): FRIDAY is not a sports
betting system. FRIDAY_EXCLUDED_SEGMENTS (default "sports") removes those
markets from the scan, the universe, signals, edges and the calibration
backfill. Games resolve in hours against professionally set lines, a
15-minute scan has no speed advantage there, and many props barely trade;
the measured mispricing is in non-sports event markets. Set it to "none"
to include everything again.
"""

from __future__ import annotations

import os
import re

SEGMENTS_VERSION = "9B.2"
SEGMENTS = ("sports", "micro", "field", "binary")
DEFAULT_EXCLUDED = ("sports", "micro")


def excluded_segments() -> frozenset[str]:
    """FRIDAY_EXCLUDED_SEGMENTS: comma list of segments, or 'none'. Default: sports, micro."""
    raw = os.getenv("FRIDAY_EXCLUDED_SEGMENTS", "").strip().lower()
    if not raw:
        return frozenset(DEFAULT_EXCLUDED)
    if raw == "none":
        return frozenset()
    wanted = {s.strip() for s in raw.split(",") if s.strip()}
    unknown = wanted - set(SEGMENTS)
    if unknown:
        raise ValueError(f"unknown segments in FRIDAY_EXCLUDED_SEGMENTS: {sorted(unknown)}")
    if wanted >= set(SEGMENTS):
        raise ValueError("FRIDAY_EXCLUDED_SEGMENTS cannot exclude every segment")
    return frozenset(wanted)


# 8C.2: with sports out of scope, a leak matters more than before. Besides the
# game/prop wording, competitions and leagues are recognised by name, and
# Gamma's own sports fields are used when a raw market is at hand.
_LEAGUES = re.compile(
    r"\b(nfl|nba|wnba|mlb|nhl|mls|ufc|mma|epl|atp|wta|pga|liv golf|nascar|formula 1|f1|motogp|grand prix|"
    r"premier league|la liga|serie a|bundesliga|ligue 1|champions league|europa league|copa america|"
    r"world cup|super bowl|world series|stanley cup|ryder cup|davis cup|fa cup|grand slam|wimbledon|"
    r"roland garros|us open|australian open|tour de france|olympics?|playoffs?|championships?|tournament|"
    r"counter-strike|cs2|league of legends|dota 2?|valorant|esports?|"
    r"touchdowns?|home runs?|rushing yards|passing yards|strikeouts|"
    r"following the (first|second|third|final) round|make the cut|heisman|ballon d'or|mvp)\b", re.I)
SPORTS_FIELDS = ("sportsMarketType", "gameId", "gameStartTime")


def sports_hint_of(raw: dict) -> bool:
    """True when Gamma itself marks the market as a sports market (fields or tags)."""
    if not isinstance(raw, dict):
        return False
    if any(raw.get(k) not in (None, "", 0) for k in SPORTS_FIELDS):
        return True
    for holder in [raw] + [e for e in (raw.get("events") or []) if isinstance(e, dict)]:
        for tag in holder.get("tags") or []:
            label = (tag.get("slug") or tag.get("label") or "") if isinstance(tag, dict) else str(tag)
            if str(label).strip().lower() in ("sports", "esports"):
                return True
        if str(holder.get("category") or "").strip().lower() in ("sports", "esports"):
            return True
    return False


# 9A: market TYPE within the in-scope segments. "binary" mixes very different
# animals (a five-minute "Bitcoin Up or Down", a weekly song chart, a
# months-long election question); the backtest reports each separately so one
# pooled figure cannot hide where an edge does or does not hold. First match wins.
CATEGORIES = ("crypto_micro", "mentions", "weather", "crypto", "entertainment", "economy", "geopolitics",
              "politics", "other")
_CATEGORY_RULES = (
    ("crypto_micro", r"\bup or down\b"),
    ("mentions", r"\b(say|says|said|mention|mentions|tweet|tweets|post|posts)\b"),
    ("weather", r"\b(temperature|hurricane|rainfall|snowfall|degrees|°[cf])\b|\bhighest temp"),
    ("crypto", r"\b(bitcoin|btc|ethereum|eth|solana|xrp|dogecoin|doge|crypto|stablecoin|airdrop|fdv|"
               r"pump\.fun|memecoin|token launch|market cap)\b"),
    ("entertainment", r"\b(song|album|spotify|billboard|box office|movie|film|oscars?|grammys?|emmys?|netflix|"
                      r"youtube|mrbeast|tiktok|streams?|rotten tomatoes|opening weekend)\b"),
    ("economy", r"\b(fed|fomc|interest rates?|rate cut|bps|cpi|inflation|gdp|unemployment|jobs report|payrolls|"
                r"recession|treasury|yield|s&p|nasdaq|dow|stock|earnings|ipo|tariffs?|oil|gold|close above|"
                r"close below)\b"),
    ("geopolitics", r"\b(war|ceasefire|invade|invasion|strike|strikes|iran|israel|gaza|ukraine|russia|china|taiwan|"
                    r"north korea|nato|sanctions?|blockade|hostages?|treaty|military|troops|nuclear|missile)\b"),
    ("politics", r"\b(election|elections|president|presidential|senate|house|congress|parliament|parliamentary|"
                 r"prime minister|chancellor|governor|mayor|nominee|nomination|primary|approval|cabinet|impeach|"
                 r"supreme court|bill|veto|shutdown|trump|biden|republican|democrat|democratic|party|minister|"
                 r"referendum|moratorium|charged|indicted|convicted|pardon)\b"),
)
_CATEGORY_RE = tuple((name, re.compile(rx, re.I)) for name, rx in _CATEGORY_RULES)


def category_of(question: str | None) -> str:
    q = question or ""
    for name, rx in _CATEGORY_RE:
        if rx.search(q):
            return name
    return "other"


_SPORTS = re.compile(
    r"(\bvs\.?\s)|(^spread:)|(\bo/u\b)|(\bwin on \d{4}-\d{2}-\d{2})|(\(bo\d\))|"
    r"(\bgame \d+ winner\b)|(\bmap \d+ winner\b)|(\bset \d+ winner\b)|(\btotal (games|sets|maps|points|rounds|goals|runs|kills)\b)|"
    r"(\bboth teams to score\b)|(\bend in a draw\b)|"
    # 8C: fights and props
    r"(\bwins? by (ko|tko|ko ?/ ?tko|ko or tko|submission|decision|stoppage)\b)|(\bko ?/ ?tko\b)|"
    r"(\bgo(es)? the distance\b)|(\bin round \d\b)|(\bround \d winner\b)|(\bhandicap\b)|"
    r"(\b(1st|2nd|first|second) (half|quarter|period|inning|innings|set|map)\b)|(\bmoneyline\b)|"
    r"(\bmatch winner\b)|(\bto win the match\b)|(\bufc \d+\b)|(\bufc fight night\b)|(\bover/under \d+(\.\d+)?(?!\.?\d*\s*%))",
    re.I)


_MICRO = re.compile(r"\bup or down\b.*\d{1,2}(:\d{2})?\s*(am|pm)", re.I)


def segment_of(question: str | None, neg_risk: bool | int | None = None, sports_hint: bool = False) -> str:
    q = question or ""
    if _MICRO.search(q):
        return "micro"
    if sports_hint or _SPORTS.search(q) or _LEAGUES.search(q):
        return "sports"
    if neg_risk:
        return "field"
    return "binary"
