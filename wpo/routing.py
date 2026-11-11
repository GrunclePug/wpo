"""Tier router: work names an intelligence tier, the router picks the route that runs it.

Static routes (Claude, GPT through CLIProxyAPI, the local model) come from the policy,
`models/tiers.json` merged with the project's `routing` overrides. Free routes are rebuilt from live
sources whenever the catalog is older than `free_routes.catalog_max_age_hours`:

  OpenCode Zen   `opencode models opencode` lists the free models usable now; models.dev supplies
                 their metadata. Zen's free tier serves only the OpenCode client.
  OpenRouter     /api/v1/models priced 0 with the ':free' suffix and tool support; dispatchable only
                 with the user's key. Also run through the OpenCode CLI.

A free model is dispatched only with an intelligence score (models/scores.json, overlaid by the
Artificial Analysis API when a key exists); unrated models are listed, never guessed.

Candidate order for a tier: routes at or above the floor, free first (best first), then
subscriptions (cheapest first); then downgrade routes between the fallback floor and the floor, best
first. The local model is never ranked: it runs only where a spec pins it (the pool lane keeps it
busy on its own work), so it neither displaces nor waits behind hosted routes. "+taste" puts Claude first. Unavailable routes (usage over a ceiling, cooldown after a
failure, local server down, no key) are skipped with a reason. An optional `weekly_pace` per family
spreads a weekly budget linearly over the week instead of letting it be spent at once. Catalog and cooldowns are machine-wide
state, shared by every project.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import SKILL_ROOT
from .machine import MachineConfig
from .platform import FileLock, replace_file

MODELS_DIRECTORY = SKILL_ROOT / "models"
TIERS_FILE = MODELS_DIRECTORY / "tiers.json"
SCORES_FILE = MODELS_DIRECTORY / "scores.json"
MODELS_DEV_URL = "https://models.dev/api.json"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
ARTIFICIAL_ANALYSIS_URL = "https://artificialanalysis.ai/api/v2/language/models/free"
HTTP_TIMEOUT_SECONDS = 60
FIVE_HOUR_WINDOW_SECONDS = 18000
UNKNOWN_USAGE_RECHECK = timedelta(minutes=15)
LOCAL_DOWN_RECHECK = timedelta(minutes=15)
COOLDOWNS = {
    "subscription_rate_limited_without_visible_cap": timedelta(minutes=15),
    "zen_rate_limited": timedelta(minutes=60),
    "zen_free_usage_limit": timedelta(hours=2),
    "openrouter_rate_limited_per_minute": timedelta(minutes=10),
    "local_rate_limited": timedelta(minutes=5),
    # One xAI account serves every Grok route; its limits are per minute and per day, not published.
    "xai_rate_limited": timedelta(minutes=30),
    # Spent credits (402 spending-limit) stay spent until the account is topped up or its free
    # allowance renews; retry daily, since a spent account fails fast.
    "xai_credits_exhausted": timedelta(hours=24),
    # agy's plans refresh every 5 h or weekly; when its error names no reset time, retry hourly
    # (a spent group fails fast, so the retry is cheap).
    "agy_quota": timedelta(hours=1),
    "hang": timedelta(minutes=30),
    "api_error": timedelta(minutes=20),
    "model_gone": timedelta(hours=24),
    "session_error": timedelta(minutes=10),
}
OUTCOMES = ("rate_limited", "api_error", "hang", "session_error")
SUBSCRIPTION_FAMILIES = ("claude", "gpt")
RATE_LIMIT_TEXT = re.compile(r"\b429\b|rate.?limit|usage limit|hit your limit|limit reached|too many requests|quota|resource.?exhausted"
                             r"|credits balance is too low", re.IGNORECASE)
# agy's quota error states when the group refills: "Resets in 165h26m59s."
AGY_RESETS_IN_TEXT = re.compile(r"Resets in (?=\d)(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?", re.IGNORECASE)
MODEL_GONE_TEXT = re.compile(r"\b404\b|model not found|no such model|unknown model|not a valid model|no endpoints found", re.IGNORECASE)
ZEN_FREE_LIMIT_TEXT = re.compile(r"Free usage limit reached", re.IGNORECASE)
XAI_CREDITS_TEXT = re.compile(r"spending-limit|run out of credits", re.IGNORECASE)
DAILY_LIMIT_TEXT = re.compile(r"per.?day|daily|free-models-per-day", re.IGNORECASE)
# The Claude CLI's stream-json log marks an automatic compaction with this system event.
COMPACTION_MARKER = '"subtype":"compact_boundary"'

Emit = Callable[[str, str], object]


class RoutingError(Exception):
    """A work spec, route or source that cannot be resolved; never silently replaced."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def from_timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)


def read_json(path: Path, default: object) -> object:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_atomically(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    replace_file(temporary, path)


def paced_weekly_ceiling(window: dict, hard_ceiling: float, pace: dict, now: datetime) -> tuple[float, datetime]:
    """The weekly ceiling under a pacing budget, and when the pace next lets the window through.

    `budget_percent` of the week is spread linearly over it, plus `slack_percent` up front, so one
    project cannot spend the account's week in a day and starve every other use of it. Usage is
    account-wide, so other sessions' use counts against the pace too.
    """
    window_seconds = float(window["window_seconds"])
    resets_at = from_timestamp(float(window["resets_at"]))
    elapsed = min(1.0, max(0.0, 1.0 - (resets_at - now).total_seconds() / window_seconds))
    budget, slack = float(pace["budget_percent"]), float(pace["slack_percent"])
    ceiling = min(hard_ceiling, budget * elapsed + slack)
    used = float(window["used_percent"])
    if budget <= 0 or used >= min(hard_ceiling, budget + slack):
        return ceiling, resets_at
    # The pace reaches the current usage once the elapsed share reaches (used - slack) / budget.
    elapsed_needed = max(0.0, (used - slack) / budget)
    return ceiling, resets_at - timedelta(seconds=window_seconds * (1.0 - elapsed_needed))


def merge_policy(base: dict, override: dict) -> dict:
    """Deep-merges a project's routing overrides; `routes` replaces, `routes_extra` appends."""
    merged = dict(base)
    for key, value in override.items():
        if key == "routes_extra":
            merged["routes"] = list(base["routes"]) + list(value)
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = merge_policy(base[key], value)
        else:
            merged[key] = value
    return merged


def load_policy(override: dict | None = None) -> dict:
    return merge_policy(json.loads(TIERS_FILE.read_text(encoding="utf-8")), override or {})


# ------------------------------------------------------------------------------------- work specs


@dataclass(frozen=True)
class WorkSpec:
    """What a session asks for: a tier (optionally taste-critical) or one pinned route."""

    tier: str | None
    taste: bool
    pinned_route: str | None
    source: str


def resolve_work_spec(spec: str, policy: dict) -> WorkSpec:
    """Accepts a tier ('code-hard'), a taste tier ('frontier+taste'), a legacy routing key, or
    'pin:<route id>'."""
    text = spec.strip()
    if text.startswith("pin:"):
        return WorkSpec(tier=None, taste=False, pinned_route=text.removeprefix("pin:"), source=text)
    if text in policy.get("legacy_keys", {}):
        return dataclasses.replace(resolve_work_spec(policy["legacy_keys"][text], policy), source=text)
    taste_suffix = policy["taste_suffix"]
    taste = text.endswith(taste_suffix)
    tier = text.removesuffix(taste_suffix)
    if tier not in policy["tiers"]:
        raise RoutingError(f"unknown tier or routing key '{spec}' (tiers: {', '.join(policy['tiers'])}; or a legacy key, or pin:<route>)")
    return WorkSpec(tier=tier, taste=taste, pinned_route=None, source=text)


# ---------------------------------------------------------------------------------------- scores


def normalize_model_name(name: str) -> str:
    """Provider-free, suffix-free, punctuation-free form used to match a model to its score:
    'zhipuai/GLM-5.3-Flash', 'glm-5.3-flash-free' and 'z/glm-5.3-flash:free' all become 'glm53flash'."""
    base = name.strip().lower().rsplit("/", 1)[-1].removesuffix(":free")
    base = re.sub(r"-free$", "", base)
    base = re.sub(r"-\d{8}$", "", base)
    return re.sub(r"[^a-z0-9]", "", base)


@dataclass(frozen=True)
class Score:
    value: float
    source: str
    variant: str | None


def load_manual_scores() -> dict[str, Score]:
    table: dict[str, Score] = {}
    for entry in json.loads(SCORES_FILE.read_text(encoding="utf-8"))["models"]:
        score = Score(float(entry["score"]), f"{entry['source']} {entry['as_of']}", entry.get("variant"))
        for name in entry["names"]:
            table[normalize_model_name(name)] = score
    return table


def fetch_json(url: str, headers: dict[str, str] | None = None) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "wp-orchestration/1", **(headers or {})})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.load(response)


def score_for(names: Iterable[str | None], scores: dict[str, Score]) -> Score | None:
    for name in names:
        if name and (score := scores.get(normalize_model_name(name))):
            return score
    return None


# ----------------------------------------------------------------------------------------- routes


@dataclass
class Route:
    """One way to run a session: a harness, a model, and what it is worth."""

    id: str
    family: str
    harness: str
    model: str
    effort: str | None
    score: float | None
    writes_code: bool
    cost_class: str
    provider: str | None = None
    variant: str | None = None
    context_tokens: int | None = None
    score_source: str | None = None
    # Artificial Analysis's measures of one Intelligence Index run (snapshot in tiers.json): the
    # API price of a task, the output tokens of the whole run in millions, and the output speed in
    # tokens per second. The family frontier compares them; None where a route has no price.
    cost_per_task: float | None = None
    index_tokens_m: float | None = None
    output_speed: float | None = None

    @property
    def index_hours(self) -> float | None:
        """Hours the whole Index run spends generating: how long a task takes, relative to other routes."""
        if self.index_tokens_m is None or not self.output_speed:
            return None
        return self.index_tokens_m * 1e6 / self.output_speed / 3600
    dispatchable: bool = True
    not_dispatchable_reason: str | None = None


def static_routes(policy: dict) -> list[Route]:
    cost_classes = policy.get("family_cost_class", {})
    return [
        Route(
            id=entry["id"],
            family=entry["family"],
            harness=entry["harness"],
            model=entry["model"],
            effort=entry.get("effort"),
            score=float(entry["score"]),
            writes_code=bool(entry["writes_code"]),
            cost_class=cost_classes.get(entry["family"], "subscription"),
            # agy routes name their quota group as provider: a spent group cools all its routes.
            provider=entry.get("provider"),
            context_tokens=entry.get("context_tokens"),
            score_source=entry.get("score_source", "policy snapshot"),
            cost_per_task=entry.get("cost_per_task"),
            index_tokens_m=entry.get("index_tokens_m"),
            output_speed=entry.get("output_speed"),
        )
        for entry in policy["routes"]
    ]


def frontier_measures(route: Route) -> tuple[float, float, float] | None:
    """What a route costs per task: its price, its output tokens and its time; None when unmeasured."""
    if route.score is None or route.cost_per_task is None or route.index_tokens_m is None or route.index_hours is None:
        return None
    return (route.cost_per_task, route.index_tokens_m, route.index_hours)


def dominates(better: Route, worse: Route) -> bool:
    """Whether `better` is at least as intelligent as `worse` and no costlier, wordier or slower per
    task, and strictly ahead on one of those."""
    ahead, behind = frontier_measures(better), frontier_measures(worse)
    if ahead is None or behind is None or better.score < worse.score:
        return False
    if any(mine > theirs for mine, theirs in zip(ahead, behind)):
        return False
    return better.score > worse.score or ahead != behind


def family_frontiers(routes: list[Route]) -> list[Route]:
    """The routes the tiers rank: each family's Pareto frontier of intelligence against price,
    tokens and time per task, and their union across families. A route another of its family
    dominates is dropped (a pin still reaches it). Price alone is not enough: Haiku at max is
    cheaper than Opus at low and about as smart, but writes twenty times the tokens and takes far
    longer, so both stay. Routes without the measures (free and local) are not compared and stay."""
    return [route for route in routes
            if not any(other.family == route.family and other is not route and dominates(other, route) for other in routes)]


@dataclass
class Availability:
    available: bool
    reason: str | None = None
    available_at: datetime | None = None
    # Why an available family may serve a tier only one step down (its downgrade band), if it is
    # over its weekly pace but within the band past it.
    cheaper_only: str | None = None


@dataclass
class Selection:
    spec: str
    tier: str | None
    candidates: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    next_available_at: float | None = None


@dataclass
class SessionOutcome:
    outcome: str  # ok | rate_limited | api_error | session_error
    detail: str
    result_text: str
    # Why the session was asked for its handoff while it ran (session.HandoffTrigger), if it was.
    handoff_reason: str | None = None


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


class Router:
    """Route selection, failure marks and the free catalog for one project's policy."""

    def __init__(self, machine: MachineConfig, policy_override: dict | None = None, emit: Emit | None = None, project=None) -> None:
        self.machine = machine
        self.project = project
        self.policy = merge_policy(load_policy(machine.raw.get("routing")), policy_override or {})
        self.emit = emit or (lambda kind, message: None)
        self.state_dir = machine.state_dir / "router"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def families(self) -> tuple[str, ...]:
        return tuple(self.policy["families"])

    # ------------------------------------------------------------------------------ free catalog

    @property
    def catalog_path(self) -> Path:
        return self.state_dir / "catalog.json"

    @property
    def artificial_analysis_cache_path(self) -> Path:
        """Last successful Artificial Analysis fetch; reused when a refresh fails (e.g. the free tier's daily limit)."""
        return self.state_dir / "artificial-analysis-scores.json"

    def openrouter_key(self) -> str | None:
        if os.environ.get("OPENROUTER_API_KEY"):
            return os.environ["OPENROUTER_API_KEY"]
        key_file = self.machine.key_file("openrouter")
        return (key_file.read_text(encoding="utf-8").strip() or None) if key_file.exists() else None

    def fetch_artificial_analysis_scores(self) -> dict[str, Score]:
        key_file = self.machine.key_file("artificial_analysis")
        if not key_file.exists():
            return {}
        headers = {"x-api-key": key_file.read_text(encoding="utf-8").strip()}
        models: list[dict] = []
        page = 1
        while True:
            payload = fetch_json(f"{ARTIFICIAL_ANALYSIS_URL}?page={page}", headers)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise RoutingError(f"Artificial Analysis page {page} has no 'data' list")
            models.extend(payload["data"])
            if not (payload.get("pagination") or {}).get("has_more"):
                break
            page += 1
        stamp = now_utc().date().isoformat()
        table: dict[str, Score] = {}
        for model in models:
            index = (model.get("evaluations") or {}).get("artificial_analysis_intelligence_index")
            if index is None:
                continue
            for name in (model.get("slug"), model.get("name")):
                if name:
                    table[normalize_model_name(name)] = Score(float(index), f"Artificial Analysis API {stamp}", None)
        return table

    def zen_routes(self, models_dev: dict, scores: dict[str, Score], min_context: int) -> tuple[list[Route], list[str]]:
        executable = self.machine.executable("opencode")
        completed = subprocess.run([executable, "models", "opencode"], capture_output=True, text=True, timeout=180)
        if completed.returncode != 0:
            raise RoutingError(f"opencode models failed ({completed.returncode}): {completed.stderr.strip()[:300]}")
        model_ids = [line.strip().removeprefix("opencode/") for line in completed.stdout.splitlines() if line.strip().startswith("opencode/")]
        metadata = models_dev.get("opencode", {}).get("models", {})
        routes, unrated = [], []
        for model_id in model_ids:
            model = metadata.get(model_id)
            if model is None:
                unrated.append(f"zen/{model_id} (not on models.dev)")
                continue
            cost = model.get("cost", {})
            if cost.get("input") != 0 or cost.get("output") != 0:
                continue
            route_id = f"zen/{model_id}"
            score = score_for((model.get("canonical_model_id"), model_id, model.get("name")), scores)
            if score is None:
                unrated.append(route_id)
                continue
            context = int(model.get("limit", {}).get("context", 0))
            reasons = [reason for reason, applies in (("no tool calling", not model.get("tool_call")), (f"context {context} below {min_context}", context < min_context)) if applies]
            variant = score.variant if score.variant and score.variant in (model.get("reasoning_options") or []) else None
            routes.append(Route(
                id=route_id, family="free", harness="opencode", model=f"opencode/{model_id}", effort=None, score=score.value,
                writes_code=True, cost_class="free", provider="zen", variant=variant, context_tokens=context,
                score_source=score.source, dispatchable=not reasons, not_dispatchable_reason="; ".join(reasons) or None,
            ))
        return routes, unrated

    def openrouter_routes(self, scores: dict[str, Score], min_context: int) -> tuple[list[Route], list[str]]:
        payload = fetch_json(OPENROUTER_MODELS_URL)
        has_key = self.openrouter_key() is not None
        routes, unrated = [], []
        for model in payload["data"]:
            model_id = model["id"]
            pricing = model.get("pricing", {})
            if not model_id.endswith(":free") or float(pricing.get("prompt", 1)) != 0 or float(pricing.get("completion", 1)) != 0:
                continue
            if "tools" not in (model.get("supported_parameters") or []):
                continue
            route_id = f"openrouter/{model_id}"
            score = score_for((model.get("canonical_slug"), model_id, model.get("name")), scores)
            if score is None:
                unrated.append(route_id)
                continue
            context = int(model.get("context_length") or 0)
            reasons = []
            if not has_key:
                reasons.append(f"no OpenRouter key (set OPENROUTER_API_KEY or write {self.machine.key_file('openrouter')})")
            if context < min_context:
                reasons.append(f"context {context} below {min_context}")
            routes.append(Route(
                id=route_id, family="free", harness="opencode", model=f"openrouter/{model_id}", effort=None, score=score.value,
                writes_code=True, cost_class="free", provider="openrouter", context_tokens=context, score_source=score.source,
                dispatchable=not reasons, not_dispatchable_reason="; ".join(reasons) or None,
            ))
        return routes, unrated

    def refresh_catalog(self) -> dict:
        """Rebuilds the free catalog. A failing source is reported and its routes dropped."""
        min_context = int(self.policy["free_routes"]["min_context_tokens"])
        scores = load_manual_scores()
        errors: list[str] = []
        try:
            api_scores = self.fetch_artificial_analysis_scores()
            write_atomically(self.artificial_analysis_cache_path, {name: asdict(score) for name, score in api_scores.items()})
        except (OSError, ValueError, RoutingError) as error:
            api_scores = {name: Score(**score) for name, score in read_json(self.artificial_analysis_cache_path, {}).items()}
            errors.append(f"Artificial Analysis: {error}; using {len(api_scores)} cached scores from the last successful fetch")
        scores |= api_scores
        routes: list[Route] = []
        unrated: list[str] = []
        try:
            zen, zen_unrated = self.zen_routes(fetch_json(MODELS_DEV_URL), scores, min_context)
            routes += zen
            unrated += zen_unrated
        except (OSError, ValueError, KeyError, RoutingError, subprocess.TimeoutExpired) as error:
            errors.append(f"OpenCode Zen: {error}")
        try:
            openrouter, openrouter_unrated = self.openrouter_routes(scores, min_context)
            routes += openrouter
            unrated += openrouter_unrated
        except (OSError, ValueError, KeyError) as error:
            errors.append(f"OpenRouter: {error}")
        previous = read_json(self.catalog_path, {})
        catalog = {"refreshed_at": now_utc().timestamp(), "routes": [asdict(route) for route in routes], "unrated": sorted(unrated), "errors": errors}
        write_atomically(self.catalog_path, catalog)
        dispatchable = [route.id for route in routes if route.dispatchable]
        self.emit("catalog-refreshed", f"{len(dispatchable)} dispatchable free routes: {', '.join(dispatchable) or 'none'}")
        if errors:
            self.emit("catalog-refresh-failed", "; ".join(errors))
        newly_unrated = sorted(set(unrated) - set(previous.get("unrated", [])))
        if newly_unrated:
            self.emit("catalog-unrated", f"free models without an intelligence score (add them to {SCORES_FILE} to use them): {', '.join(newly_unrated)}")
        return catalog

    def load_catalog(self, force_refresh: bool = False) -> dict:
        max_age = timedelta(hours=float(self.policy["free_routes"]["catalog_max_age_hours"]))

        def fresh(catalog: dict | None) -> bool:
            return catalog is not None and not catalog.get("stale") and now_utc() - from_timestamp(catalog["refreshed_at"]) <= max_age

        catalog = read_json(self.catalog_path, None)
        if fresh(catalog) and not force_refresh:
            return catalog
        with FileLock(self.state_dir / "catalog.lock", timeout_seconds=600):
            catalog = read_json(self.catalog_path, None)
            if fresh(catalog) and not force_refresh:
                return catalog
            return self.refresh_catalog()

    def catalog_routes(self, force_refresh: bool = False) -> list[Route]:
        if self.machine.execution_mode == "t3":
            # Discovery through standalone clients is not execution through a native T3 provider.
            # Keep existing catalog data readable; native capability checks below decide availability.
            return [Route(**entry) for entry in read_json(self.catalog_path, {"routes": []})["routes"]]
        if not self.policy["free_routes"].get("enabled", True):
            return []
        return [Route(**entry) for entry in self.load_catalog(force_refresh)["routes"]]

    # ------------------------------------------------------------------------------ availability

    @property
    def health_path(self) -> Path:
        return self.state_dir / "route-health.json"

    def load_health(self) -> dict:
        return read_json(self.health_path, {"cooldowns": {}})

    def usage_windows(self, refresh: bool = True) -> dict[str, list[dict] | None]:
        """Usage windows per subscription family from usage_budget.py's cache; None = unknown.
        `refresh` False only reads the cache, however old: an observer (the dashboard) must neither
        fetch usage nor take the refresh lock the supervisors wait on."""
        cache = self.machine.usage_cache

        def stale(snapshot: dict | None) -> bool:
            return snapshot is None or time.time() - float(snapshot["fetched_at"]) > self.machine.usage_max_age_seconds

        snapshot = read_json(cache, None)
        if refresh and stale(snapshot) and self.machine.usage_script.exists():
            # Every supervisor finds the cache stale at the same moment; one fetches, the others read
            # its result, so the usage endpoints see one request per refresh, not one per lane.
            with FileLock(self.state_dir / "usage-refresh.lock"):
                if stale(snapshot := read_json(cache, None)):
                    try:
                        subprocess.run([self.machine.python, "-I", str(self.machine.usage_script)], capture_output=True, timeout=120)
                    except subprocess.TimeoutExpired:
                        pass  # a stale or missing cache below means usage unknown, which blocks safely
                    snapshot = read_json(cache, None)
        if snapshot is None:
            return {family: None for family in SUBSCRIPTION_FAMILIES}
        return {family["family"]: (None if family.get("error") else list(family["windows"])) for family in snapshot["families"]}

    def cap_forecast(self, family: str) -> CapForecast | None:
        """The family's usage window that will reach its cap first at the recent burn rate, or None
        while no window is burning. The rate is the average slope of the window's readings over
        `burn_forecast.lookback_minutes` (a short moving average, so a burst of parallel sessions
        shows within minutes); the time left counts from the latest reading, which may be minutes old."""
        settings = self.policy["burn_forecast"]
        history_path = self.machine.usage_history
        if family not in SUBSCRIPTION_FAMILIES or not history_path.exists():
            return None
        now = time.time()
        lookback = float(settings["lookback_minutes"]) * 60
        rows = []
        for line in history_path.read_text(encoding="utf-8").splitlines():
            try:  # the hook appends while we read: a torn last line is skipped
                row = json.loads(line)
            except ValueError:
                continue
            if row["family"] == family and now - float(row["ts"]) <= lookback:
                rows.append(row)
        forecasts = []
        for label in {row["label"] for row in rows}:
            latest = max((row for row in rows if row["label"] == label), key=lambda row: float(row["ts"]))
            # Only readings of the same window instance: a reset drops the percentage to zero.
            same = [row for row in rows if row["label"] == label and abs(float(row["resets_at"]) - float(latest["resets_at"])) < 120]
            oldest = min(same, key=lambda row: float(row["ts"]))
            span_minutes = (float(latest["ts"]) - float(oldest["ts"])) / 60
            if span_minutes < float(settings["min_span_minutes"]):
                continue
            rate = (float(latest["used_percent"]) - float(oldest["used_percent"])) / span_minutes
            if rate <= 0 or float(latest["resets_at"]) <= now:
                continue
            minutes_left = (100 - float(latest["used_percent"])) / rate - (now - float(latest["ts"])) / 60
            forecasts.append(CapForecast(family, label, float(latest["used_percent"]), rate, minutes_left))
        return min(forecasts, key=lambda forecast: forecast.minutes_to_cap, default=None)

    def burning_out(self, family: str) -> CapForecast | None:
        """The forecast when a window reaches its cap within `burn_forecast.lead_minutes`."""
        forecast = self.cap_forecast(family)
        return forecast if forecast is not None and forecast.minutes_to_cap <= float(self.policy["burn_forecast"]["lead_minutes"]) else None

    def subscription_blocked_until(self, windows: list[dict] | None, family: str, paced: bool = True) -> tuple[datetime, str | None]:
        """When a subscription family may start sessions again: its windows under the fixed
        ceilings, the weekly pace (unless `paced` is False: an explicit pin is the operator's call
        on the week), and no window burning toward its cap. Within the pace's `cheaper_band_percent`
        past it the family is not blocked; `cheaper_only` says so."""
        blocked_until, reason, _ = self.subscription_state(windows, family, paced)
        return blocked_until, reason

    def subscription_state(self, windows: list[dict] | None, family: str, paced: bool = True) -> tuple[datetime, str | None, str | None]:
        """`subscription_blocked_until`, with why the family may serve tiers only one step down.

        The weekly pace is graded rather than a wall: under it a family serves every tier at its
        floor; up to `cheaper_band_percent` (default: the pace's slack) past it, only from the tier's
        downgrade band, so work goes on at a lower effort while the pace catches up; beyond that
        the family waits for the pace."""
        now = now_utc()
        if windows is None:
            return now + UNKNOWN_USAGE_RECHECK, "usage unknown", None
        ceilings = self.policy["ceilings"][family]
        pace = self.policy.get("weekly_pace", {}).get(family) if paced else None
        blocked_until, reasons, cheaper = now, [], []
        for window in windows:
            resets_at = from_timestamp(float(window["resets_at"]))
            if window["window_seconds"] <= FIVE_HOUR_WINDOW_SECONDS:
                ceiling, unblocks_at = ceilings["five_hour"], resets_at
            elif pace:
                ceiling, unblocks_at = paced_weekly_ceiling(window, ceilings["weekly"], pace, now)
                band = float(pace.get("cheaper_band_percent", pace["slack_percent"]))
                if ceiling <= window["used_percent"] < min(float(ceilings["weekly"]), ceiling + band):
                    cheaper.append(f"{window['label']} {window['used_percent']}% is over the weekly pace ({ceiling:.0f}%)")
                    continue
            else:
                ceiling, unblocks_at = ceilings["weekly"], resets_at
            if window["used_percent"] < ceiling:
                continue
            reasons.append(f"{window['label']} {window['used_percent']}% >= {ceiling:.0f}%{' (weekly pace)' if pace and unblocks_at < resets_at else ''}")
            blocked_until = max(blocked_until, unblocks_at)
        if forecast := self.burning_out(family):
            # A family burning toward its cap starts nothing new: its running sessions hand off,
            # and the rate is looked at again once the lead time has passed.
            reasons.append(forecast.text)
            blocked_until = max(blocked_until, now + timedelta(minutes=float(self.policy["burn_forecast"]["lead_minutes"])))
        return blocked_until, ("; ".join(reasons) or None), ("; ".join(cheaper) or None)

    def oracle(self, paced: bool = True) -> AvailabilityOracle:
        return AvailabilityOracle(self, paced)

    # --------------------------------------------------------------------------------- selection

    def ordering_key(self, route: Route, taste: bool) -> tuple:
        cost_rank = self.policy["cost_class_order"].index(route.cost_class)
        family_order = self.policy["taste_family_order"] if taste else self.policy["subscription_family_order"]
        family_rank = family_order.index(route.family) if route.family in family_order else 0
        taste_rank = 0 if taste and route.family == "claude" else 1
        # Free routes cost nothing, so the smartest goes first; paid routes go cheapest-first, by
        # their price per task where it is measured (every subscription route has one).
        if route.cost_class == "free":
            score_rank = -route.score
        else:
            score_rank = route.cost_per_task if route.cost_per_task is not None else route.score
        return (taste_rank, cost_rank, score_rank, family_rank, route.id)

    def all_routes(self, families: Iterable[str], force_refresh: bool = False) -> list[Route]:
        routes = static_routes(self.policy)
        if "free" in families:
            routes += self.catalog_routes(force_refresh)
        return routes

    def select(self, spec_text: str, families: Iterable[str] | None = None, force_refresh: bool = False,
               free_last_resort: bool = True, gate: bool = False) -> Selection:
        """Ranks the routes for a work spec. With `free_last_resort`, free routes at or above the
        policy's `free_last_resort.floor` come last for every tier, so work proceeds on a free model
        instead of waiting while every paid route is capped, paced or cooling down. Reviews pass
        False: they are the gate into main. Local routes are only reachable by a pin. A pin skips the weekly pace, never the ceilings.
        Routes at a `pin_only_efforts` effort (max), and the `pin_only_routes` a project sets aside, are reachable only by a pin; the efforts never for a
        `gate` session (a review or re-evaluation), pinned or not."""
        spec = resolve_work_spec(spec_text, self.policy)
        pin_only_efforts = set(self.policy.get("pin_only_efforts", []))
        pin_only_routes = set(self.policy.get("pin_only_routes", []))
        allowed = set(families or self.families)
        unknown = allowed - set(self.families)
        if unknown:
            raise RoutingError(f"unknown families {sorted(unknown)}; known: {', '.join(self.families)}")
        routes = self.all_routes(allowed, force_refresh)
        oracle = self.oracle(paced=not spec.pinned_route)
        selection = Selection(spec=spec.source, tier=spec.tier)
        if spec.pinned_route:
            pinned = [route for route in routes if route.id == spec.pinned_route]
            if not pinned:
                raise RoutingError(f"pinned route '{spec.pinned_route}' is not a known route")
            if gate and pinned[0].effort in pin_only_efforts and pinned[0].family != "local":
                raise RoutingError(f"reviews and re-evaluations never run at {pinned[0].effort} effort; '{spec.pinned_route}' cannot be pinned for one")
            ranked = [(pinned[0], False)]
        else:
            tier = self.policy["tiers"][spec.tier]
            eligible = [route for route in family_frontiers(routes) if route.family in allowed and route.family != "local"
                        and route.effort not in pin_only_efforts and route.id not in pin_only_routes and (route.writes_code or not tier["writes_code"])]
            qualified = sorted((r for r in eligible if r.score >= tier["floor"]), key=lambda r: self.ordering_key(r, spec.taste))
            downgrades = sorted((r for r in eligible if tier["fallback_floor"] <= r.score < tier["floor"]), key=lambda r: -r.score)
            ranked = [(route, False) for route in qualified] + [(route, True) for route in downgrades]
            last_resort = self.policy["free_last_resort"]
            if free_last_resort and last_resort["enabled"]:
                ranked_ids = {route.id for route, _ in ranked}
                ranked += [(route, True) for route in sorted(eligible, key=lambda r: -r.score)
                           if route.cost_class == "free" and route.score >= last_resort["floor"] and route.id not in ranked_ids]
        earliest: datetime | None = None
        for route, downgrade in ranked:
            availability = oracle.route(route)
            if availability.available and availability.cheaper_only and not downgrade:
                # Over its weekly pace the family still serves the tier, one step down its frontier.
                selection.skipped.append({"id": route.id, "reason": f"{availability.cheaper_only}: only routes below the tier floor"})
                continue
            if availability.available:
                selection.candidates.append({**asdict(route), "downgrade": downgrade})
                continue
            selection.skipped.append({"id": route.id, "reason": availability.reason})
            if availability.available_at and (earliest is None or availability.available_at < earliest):
                earliest = availability.available_at
        if not selection.candidates:
            selection.next_available_at = earliest.timestamp() if earliest else None
        return selection

    # ------------------------------------------------------------------------------------- marks

    def find_route(self, route_id: str) -> Route:
        routes = static_routes(self.policy) + [Route(**entry) for entry in read_json(self.catalog_path, {"routes": []})["routes"]]
        match = next((route for route in routes if route.id == route_id), None)
        if match is None:
            raise RoutingError(f"unknown route '{route_id}'")
        return match

    def cooldown_for(self, route: Route, outcome: str, detail: str) -> tuple[str, datetime]:
        """Which key to cool down, and until when, after a failed session."""
        now = now_utc()
        if route.harness == "xai" and XAI_CREDITS_TEXT.search(detail):
            # The proxy reports spent credits as 402 or, once it drops the auth, as 503; behind a
            # refusing proxy the session is reported rate limited or hung. Credits stay spent
            # whatever the outcome, so this comes first (2026-10-27: a 15-minute rate-limit
            # cooldown let every lane retry Grok and hang for 10 minutes each time).
            return "provider:xai", now + COOLDOWNS["xai_credits_exhausted"]
        if outcome == "rate_limited":
            if route.family in SUBSCRIPTION_FAMILIES:
                blocked_until, _ = self.subscription_blocked_until(self.usage_windows().get(route.family), route.family)
                until = blocked_until if blocked_until > now else now + COOLDOWNS["subscription_rate_limited_without_visible_cap"]
                return f"family:{route.family}", until
            if route.provider == "openrouter" and DAILY_LIMIT_TEXT.search(detail):
                tomorrow = now.date() + timedelta(days=1)
                return "provider:openrouter", datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)
            if route.provider == "openrouter":
                return f"route:{route.id}", now + COOLDOWNS["openrouter_rate_limited_per_minute"]
            if route.provider == "zen" and ZEN_FREE_LIMIT_TEXT.search(detail):
                return "provider:zen", now + COOLDOWNS["zen_free_usage_limit"]
            if route.provider == "zen":
                return f"route:{route.id}", now + COOLDOWNS["zen_rate_limited"]
            if route.harness == "xai":
                return "provider:xai", now + COOLDOWNS["xai_rate_limited"]
            if route.harness == "agy":
                # One quota per group (Gemini; Claude and GPT-OSS): retry the group later, not each route.
                return f"provider:{route.provider}", now + agy_quota_wait(detail)
            return f"route:{route.id}", now + COOLDOWNS["local_rate_limited"]
        if outcome == "hang" and route.family in SUBSCRIPTION_FAMILIES:
            # A subscription session that stalls is the family's service stalling (a silent cap, a
            # retrying proxy), not one model: trying each of its routes for the idle limit in turn
            # cost 46 sessions of 30 minutes on 2026-10-25. The whole family waits instead.
            return f"family:{route.family}", now + COOLDOWNS["hang"]
        if outcome == "api_error" and route.cost_class == "free" and MODEL_GONE_TEXT.search(detail):
            return f"route:{route.id}", now + COOLDOWNS["model_gone"]
        return f"route:{route.id}", now + COOLDOWNS[outcome]

    def mark(self, route_id: str, outcome: str, detail: str) -> dict:
        if outcome not in OUTCOMES:
            raise RoutingError(f"unknown outcome '{outcome}'; known: {', '.join(OUTCOMES)}")
        route = self.find_route(route_id)
        key, until = self.cooldown_for(route, outcome, detail)
        with FileLock(self.state_dir / "route-health.lock"):
            health = self.load_health()
            health["cooldowns"][key] = {"until": until.timestamp(), "reason": f"{outcome}: {detail[:300]}", "route": route_id,
                                        "outcome": outcome, "since": now_utc().timestamp()}
            health["cooldowns"] = {k: v for k, v in health["cooldowns"].items() if from_timestamp(v["until"]) > now_utc()}
            write_atomically(self.health_path, health)
        if route.cost_class == "free" and outcome == "api_error" and MODEL_GONE_TEXT.search(detail):
            catalog = read_json(self.catalog_path, None)
            if catalog is not None:
                catalog["stale"] = True
                write_atomically(self.catalog_path, catalog)
        return {"key": key, "until": until.timestamp()}

    def family_cut_off(self, family: str, since: float) -> str | None:
        """Why a subscription family stopped answering at or after `since` (epoch seconds), or None:
        a usage window at 100% that has not reset, or another session's rate limit on the family
        recorded since then. A cap ends every session of the family at once, so a session still
        running on it is assumed dead."""
        if family not in SUBSCRIPTION_FAMILIES:
            return None
        now = now_utc()
        for window in self.usage_windows().get(family) or []:
            if float(window["used_percent"]) >= 100 and from_timestamp(float(window["resets_at"])) > now:
                return f"the {family} {window['label']} usage window is at {window['used_percent']}% until {from_timestamp(float(window['resets_at'])).astimezone():%H:%M}"
        entry = self.load_health()["cooldowns"].get(f"family:{family}")
        if entry and entry.get("outcome") == "rate_limited" and float(entry.get("since", 0)) >= since and from_timestamp(entry["until"]) > now:
            return f"another {family} session hit its cap ({entry['reason'][:160]})"
        return None

    def clear_cooldowns(self, key_prefix: str = "") -> list[str]:
        """Drops cooldowns whose key starts with the prefix; for when a cap was lifted early."""
        with FileLock(self.state_dir / "route-health.lock"):
            health = self.load_health()
            cleared = [key for key in health["cooldowns"] if key.startswith(key_prefix)]
            for key in cleared:
                del health["cooldowns"][key]
            write_atomically(self.health_path, health)
        return cleared

    # ------------------------------------------------------------------------------------ status

    def status_text(self) -> str:
        lines = []
        if self.policy["free_routes"].get("enabled", True):
            catalog = self.load_catalog()
            lines.append(f"free catalog refreshed {from_timestamp(catalog['refreshed_at']).astimezone():%Y-%m-%d %H:%M}")
            if catalog.get("errors"):
                lines.append("catalog errors: " + "; ".join(catalog["errors"]))
            for entry in catalog["routes"]:
                route = Route(**entry)
                state = "ready" if route.dispatchable else f"not dispatchable: {route.not_dispatchable_reason}"
                lines.append(f"  {route.id:<55} score {route.score:>5}  {state}")
            if catalog.get("unrated"):
                lines.append("unrated free models (never dispatched): " + ", ".join(catalog["unrated"]))
        for key, entry in self.load_health()["cooldowns"].items():
            until = from_timestamp(entry["until"])
            if until > now_utc():
                lines.append(f"cooldown {key} until {until.astimezone():%Y-%m-%d %H:%M}: {entry['reason'][:160]}")
        lines.append("first choices per tier:")
        for tier in self.policy["tiers"]:
            for spec in (tier, tier + self.policy["taste_suffix"]):
                selection = self.select(spec)
                picks = [candidate["id"] + (" (downgrade)" if candidate["downgrade"] else "") for candidate in selection.candidates[:3]]
                if picks:
                    lines.append(f"  {spec:<20} {' > '.join(picks)}")
                else:
                    wake = from_timestamp(selection.next_available_at).astimezone().strftime("%H:%M") if selection.next_available_at else "unknown"
                    lines.append(f"  {spec:<20} NO ROUTE (next at {wake})")
        return "\n".join(lines)


@dataclass(frozen=True)
class CapForecast:
    """When a subscription's usage window reaches its cap at the recent burn rate."""

    family: str
    label: str
    used_percent: float
    percent_per_minute: float
    minutes_to_cap: float

    @property
    def text(self) -> str:
        return (f"the {self.family} {self.label} usage window is at {self.used_percent:g}% and burning {self.percent_per_minute:.2f}%/min: "
                f"about {max(0.0, self.minutes_to_cap):.0f} min to its cap")


class AvailabilityOracle:
    """Answers whether a route can start now, memoizing per-family facts for one selection."""

    def __init__(self, router: Router, paced: bool = True) -> None:
        self.router = router
        self.paced = paced
        self.health = router.load_health()
        self._family_cache: dict[str, Availability] = {}
        self._windows: dict[str, list[dict] | None] | None = None

    def cooldown(self, key: str) -> Availability | None:
        entry = self.health["cooldowns"].get(key)
        if entry is None or from_timestamp(entry["until"]) <= now_utc():
            return None
        until = from_timestamp(entry["until"])
        return Availability(False, f"cooldown until {until.astimezone():%Y-%m-%d %H:%M} ({entry['reason']})", until)

    def family(self, family: str) -> Availability:
        if family in self._family_cache:
            return self._family_cache[family]
        if family in SUBSCRIPTION_FAMILIES:
            if self._windows is None:
                self._windows = self.router.usage_windows()
            blocked_until, reason, cheaper_only = self.router.subscription_state(self._windows.get(family), family, self.paced)
            result = Availability(True, cheaper_only=cheaper_only) if blocked_until <= now_utc() else Availability(False, reason, blocked_until)
        elif family == "local":
            machine = self.router.machine
            up = port_open(int(machine.local("model_port"))) and port_open(int(machine.local("shim_port")))
            result = Availability(True) if up else Availability(False, "local model or compaction shim not listening", now_utc() + LOCAL_DOWN_RECHECK)
        else:
            result = Availability(True)
        self._family_cache[family] = self.cooldown(f"family:{family}") or result
        return self._family_cache[family]

    def route(self, route: Route) -> Availability:
        if self.router.machine.execution_mode == "t3":
            from types import SimpleNamespace
            from .t3 import Bridge, T3Error
            if self.router.project is None:
                return Availability(False, "T3 routing requires a project context")
            try:
                Bridge(SimpleNamespace(project=self.router.project, machine=self.router.machine)).target(asdict(route))
            except T3Error as error:
                return Availability(False, str(error))
        if not route.dispatchable:
            return Availability(False, route.not_dispatchable_reason)
        family = self.family(route.family)
        if not family.available:
            return family
        if route.provider and (cooled := self.cooldown(f"provider:{route.provider}")):
            return cooled
        # The family's availability carries its pace grade (cheaper_only) to the route.
        return self.cooldown(f"route:{route.id}") or family


# ----------------------------------------------------------------------------------- classification


def json_lines(log_text: str) -> list[dict]:
    events = []
    for line in log_text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def classify_claude(log_text: str, exit_code: int) -> SessionOutcome:
    """The Claude CLI in stream-json mode ends with one 'result' event; an API failure shows as
    is_error with 'API Error: ...' text or an api_error_status."""
    results = [event for event in json_lines(log_text) if event.get("type") == "result"]
    if not results:
        api_line = next((line for line in reversed(log_text.splitlines()) if line.strip().startswith("API Error")), None)
        if api_line:
            return SessionOutcome("rate_limited" if RATE_LIMIT_TEXT.search(api_line) else "api_error", api_line.strip(), "")
        tail = " | ".join(line.strip() for line in log_text.splitlines()[-5:] if line.strip())
        return SessionOutcome("session_error", f"no result event (exit {exit_code}): {tail[:400]}", "")
    result = results[-1]
    text = str(result.get("result") or "")
    if not result.get("is_error"):
        return SessionOutcome("ok", result.get("subtype", "success"), text)
    status = result.get("api_error_status")
    detail = f"{result.get('subtype')}: status {status}: {text[:400]}"
    if status == 429 or RATE_LIMIT_TEXT.search(text):
        return SessionOutcome("rate_limited", detail, text)
    if status is not None or text.startswith("API Error"):
        return SessionOutcome("api_error", detail, text)
    return SessionOutcome("session_error", detail, text)


def classify_opencode(log_text: str, exit_code: int) -> SessionOutcome:
    """OpenCode `run --format json` emits typed events; failures are 'error' events and exit 1."""
    events = json_lines(log_text)
    errors = [event for event in events if event.get("type") == "error"]
    texts_by_message: dict[str, list[str]] = {}
    for event in events:
        if event.get("type") == "text":
            part = event.get("part", {})
            texts_by_message.setdefault(part.get("messageID", ""), []).append(part.get("text", ""))
    final_text = "".join(list(texts_by_message.values())[-1]) if texts_by_message else ""
    if errors:
        error = errors[-1].get("error", {})
        data = error.get("data", {})
        message = str(data.get("message") or error.get("name") or "unknown error")
        status = data.get("statusCode")
        detail = f"{error.get('name')}: status {status}: {message[:400]}"
        if status == 429 or RATE_LIMIT_TEXT.search(message):
            return SessionOutcome("rate_limited", detail, final_text)
        return SessionOutcome("api_error", detail, final_text)
    if exit_code != 0:
        tail = " | ".join(line.strip() for line in log_text.splitlines()[-5:] if line.strip())
        return SessionOutcome("session_error", f"exit {exit_code} without an error event: {tail[:400]}", final_text)
    if not events:
        return SessionOutcome("session_error", "no events in the log", "")
    return SessionOutcome("ok", "completed", final_text)


def agy_quota_wait(detail: str) -> timedelta:
    """How long until a spent agy group refills: its stated reset time, else the hourly retry."""
    match = AGY_RESETS_IN_TEXT.search(detail)
    if match is None or not any(match.group(name) for name in ("hours", "minutes", "seconds")):
        return COOLDOWNS["agy_quota"]
    hours, minutes, seconds = (int(match.group(name) or 0) for name in ("hours", "minutes", "seconds"))
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def classify_agy(log_text: str, exit_code: int) -> SessionOutcome:
    """agy's stream-json ends with {"event": "result", "result": {"status", "response", ...}};
    failures are status ERROR or a non-zero exit (3 for model and quota errors, stderr AGY_ERROR)."""
    results = [event["result"] for event in json_lines(log_text) if event.get("event") == "result" and isinstance(event.get("result"), dict)]
    if not results:
        tail = " | ".join(line.strip() for line in log_text.splitlines()[-5:] if line.strip())
        return SessionOutcome("session_error", f"no result event (exit {exit_code}): {tail[:400]}", "")
    result = results[-1]
    text = str(result.get("response") or "")
    if result.get("status") == "SUCCESS" and exit_code == 0:
        return SessionOutcome("ok", "completed", text)
    detail = f"status {result.get('status')}, exit {exit_code}: {str(result.get('error') or text)[:400]}"
    return SessionOutcome("rate_limited" if RATE_LIMIT_TEXT.search(detail) else "api_error", detail, text)


def classify(harness: str, log_text: str, exit_code: int) -> SessionOutcome:
    if harness in ("claude", "gpt", "xai", "local", "command"):
        return classify_claude(log_text, exit_code)
    if harness == "opencode":
        return classify_opencode(log_text, exit_code)
    if harness == "agy":
        return classify_agy(log_text, exit_code)
    raise RoutingError(f"unknown harness '{harness}'")
