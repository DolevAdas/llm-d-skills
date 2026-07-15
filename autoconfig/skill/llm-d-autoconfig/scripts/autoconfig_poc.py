"""Renderer for llm-d EPP scheduler config + benchmark workload YAML.

- The agent (SKILL Phase 2) reads upstream docs and decides which plugins,
  weights, and scheduling profiles to use. The agent emits those choices in
  `input.recommendation`. This script just RENDERS what the agent chose.
- The script also computes deterministic parameter values from user inputs
  (e.g., ttftWeight from SLA + OSL). These are derivations the agent
  shouldn't have to redo; cited in code comments.
- Sensible canonical defaults (optimized-baseline values.yaml for agg,
  pd-disaggregation values.yaml for PD) apply when the agent doesn't
  populate `input.recommendation`.

Per-feature URL map is in skill/llm-d-autoconfig/feature_docs.yaml.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Benchmark generation lives in benchmark.py; this script imports only to
# keep the CLI surface unified.
from benchmark import (
    build_benchmark,
    build_benchmark_deployment,
    _BENCHMARK_HARNESS_IMAGE,
)


VERSION = "0.3.1"
ALLOWED_PREFIX_SHARE = {"low", "medium", "high"}
ALLOWED_TOPOLOGY_MODE = {"agg", "disagg"}
ALLOWED_PD_TRANSPORT = {"rdma", "tcp"}
ALLOWED_AUTOSCALER = {None, "wva", "hpa"}
ALLOWED_SERVING_PATTERN = {"sync", "batch", "async"}
ALLOWED_DEPLOY_MODE = {"standalone", "gateway"}
# Gateway providers we know how to emit Gateway + HTTPRoute for. None means
# "user will bring their own Gateway resource"; we still emit the HTTPRoute.
ALLOWED_GATEWAY_PROVIDER = {
    None, "istio", "kgateway", "agentgateway",
    "gke-l7-rilb", "gke-l7-regional-external-managed",
}

# Workload signal labels from classify_workload(), used by benchmark.py for
# sanity rate hinting. Plugin/weight selection is the agent's job (SKILL
# Phase 2.5, from doc reads), not derived from these labels.
WORKLOAD_CLASSES = {
    "balanced-conversational",
    "high-prefix-share",
    "latency-tight",
}


# ---------------------------------------------------------------------------
# Canonical default plugins + weights
#
# These match the optimized-baseline guide's values.yaml verbatim. They're the
# fallback when the agent (SKILL Phase 2) doesn't populate
# `input.recommendation.plugins` / `.weights`. For PD or feature-enabled
# scenarios, the agent should override these from the relevant guide's
# values.yaml (read at runtime via doc_cache).
# ---------------------------------------------------------------------------

# Source: https://github.com/llm-d/llm-d/blob/main/guides/optimized-baseline/router/optimized-baseline.values.yaml
# Plugin set + per-plugin weights below are verbatim from the canonical agg
# guide's schedulingProfiles[0].plugins[].
_DEFAULT_AGG_PLUGINS: list[str] = [
    "queue-scorer",
    "kv-cache-utilization-scorer",
    "prefix-cache-scorer",
    "no-hit-lru-scorer",
]

_DEFAULT_AGG_WEIGHTS: dict[str, int] = {
    "queue-scorer": 2,
    "kv-cache-utilization-scorer": 2,
    "prefix-cache-scorer": 3,
    "no-hit-lru-scorer": 2,
}

# Source: https://github.com/llm-d/llm-d/blob/main/guides/pd-disaggregation/router/pd-disaggregation.values.yaml
# Plugin set below is verbatim from the canonical PD guide's plugins[].
_DEFAULT_PD_PLUGINS: list[str] = [
    "disagg-headers-handler",
    "always-disagg-pd-decider",
    "disagg-profile-handler",
    "prefill-filter",
    "decode-filter",
    "prefix-cache-scorer",
    "queue-scorer",
    "kv-cache-utilization-scorer",
    "active-request-scorer",
    "max-score-picker",
]

# Source: same as above; per-profile weights from the canonical
# pd-disaggregation.values.yaml schedulingProfiles[].
_DEFAULT_PD_PROFILE_WEIGHTS: dict[str, dict[str, int]] = {
    "prefill": {
        "prefix-cache-scorer": 3,
        "queue-scorer": 2,
        "kv-cache-utilization-scorer": 2,
    },
    "decode": {
        "active-request-scorer": 2,
        "prefix-cache-scorer": 3,
    },
}

_DEFAULT_PD_PROFILE_FILTERS: dict[str, str] = {
    "prefill": "prefill-filter",
    "decode": "decode-filter",
}

# Source: https://github.com/llm-d/llm-d/blob/main/guides/predicted-latency-routing/router/predicted-latency-slo.values.yaml
# Used when features.enable_latency_predictor=True AND SLO targets present.
# When no SLO, the chart's default plugins handle the producer; we just emit
# the baseline + producer overlay (see _DEFAULT_LATENCY_PREDICTOR_BASELINE).
_DEFAULT_LATENCY_PREDICTOR_SLO_PLUGINS: list[str] = [
    "queue-scorer",
    "kv-cache-utilization-scorer",
    "prefix-cache-scorer",
    "metrics-data-source",
    "core-metrics-extractor",
    "predicted-latency-producer",
    "strict-affinity-filter",
    "loose-affinity-filter",
    "latency-scorer",
    "weighted-random-picker",
    "slo-headroom-tier-filter",
    "latency-slo-admitter",
]

# Source: same predicted-latency-slo.values.yaml. The SLO profile is
# unweighted — every entry is a bare pluginRef. Order matches canonical.
_DEFAULT_LATENCY_PREDICTOR_SLO_PROFILE_REFS: list[str] = [
    "predicted-latency-producer",
    "strict-affinity-filter",
    "slo-headroom-tier-filter",
    "loose-affinity-filter",
    "latency-scorer",
    "weighted-random-picker",
]

# Source: baseline (optimized-baseline) plugin set + predicted-latency-producer
# tacked on. Used when features.enable_latency_predictor=True but no SLO
# headers are configured (= predicted-latency.values.yaml semantics).
_DEFAULT_LATENCY_PREDICTOR_BASELINE_PLUGINS: list[str] = [
    *_DEFAULT_AGG_PLUGINS,
    "predicted-latency-producer",
]

# Plugin types that the script knows go into top-level `plugins[]` but NOT
# into any schedulingProfile (they're data producers / admitters / pre-request
# handlers, wired in via featureGates).
_PLUGINS_NOT_IN_PROFILE = frozenset({
    "disagg-headers-handler",
    "always-disagg-pd-decider",
    "disagg-profile-handler",
    "latency-slo-admitter",
    "metrics-data-source",
    "core-metrics-extractor",
})

# Plugin types that take `name` (for repeated instances with different
# parameters). Currently only prefix-cache-affinity-filter is used with
# named instances (strict + loose).
# Source: predicted-latency-slo.values.yaml.
_NAMED_PLUGIN_INSTANCES: dict[str, dict] = {
    "strict-affinity-filter": {
        "type": "prefix-cache-affinity-filter",
        "parameters": {"affinityThreshold": 0.99},
    },
    "loose-affinity-filter": {
        "type": "prefix-cache-affinity-filter",
        "parameters": {"affinityThreshold": 0.80},
    },
}

# Fixed canonical parameters for plugins that need them. Source:
# predicted-latency-slo.values.yaml.
_FIXED_PLUGIN_PARAMETERS: dict[str, dict] = {
    "metrics-data-source": {
        "insecureSkipVerify": True,
        "path": "/metrics",
        "scheme": "http",
    },
    "predicted-latency-producer": {
        "streamingMode": True,
    },
}


# ---------------------------------------------------------------------------
# Input schema
# ---------------------------------------------------------------------------


@dataclass
class Workload:
    """All fields optional; missing → caller falls through to derivation defaults."""
    isl: int | None = None
    osl: int | None = None
    prefix_share: str | None = None  # low | medium | high
    prefix_len: int | None = None
    max_num_seqs: int | None = None

    def validate(self) -> None:
        if self.isl is not None:
            _require_positive(self.isl, "workload.isl")
        if self.osl is not None:
            _require_positive(self.osl, "workload.osl")
        if self.prefix_share is not None:
            _require_in(self.prefix_share, ALLOWED_PREFIX_SHARE, "workload.prefix_share")
        if self.prefix_len is not None and self.prefix_len < 0:
            raise ValueError(f"workload.prefix_len must be >= 0; got {self.prefix_len}")
        if self.max_num_seqs is not None:
            _require_positive(self.max_num_seqs, "workload.max_num_seqs")


@dataclass
class SLO:
    """All targets optional; null is a valid answer."""
    ttft_ms: int | None = None
    tpot_ms: int | None = None
    request_latency_ms: int | None = None

    def validate(self) -> None:
        for name, val in (
            ("slo.ttft_ms", self.ttft_ms),
            ("slo.tpot_ms", self.tpot_ms),
            ("slo.request_latency_ms", self.request_latency_ms),
        ):
            if val is not None:
                _require_positive(val, name)


@dataclass
class Topology:
    """Aggregated or PD-disaggregated topology.

    agg: replicas + tp (single deployment).
    disagg: prefill_{replicas,tp} + decode_{replicas,tp} + pd_transport.
    """
    mode: str
    replicas: int | None = None
    tp: int | None = None
    prefill_replicas: int | None = None
    prefill_tp: int | None = None
    decode_replicas: int | None = None
    decode_tp: int | None = None
    pd_transport: str | None = None  # rdma | tcp

    def validate(self) -> None:
        _require_in(self.mode, ALLOWED_TOPOLOGY_MODE, "topology.mode")
        if self.mode == "agg":
            if self.replicas is None or self.tp is None:
                raise ValueError(
                    "topology.replicas and topology.tp are required when mode='agg'"
                )
            _require_positive(self.replicas, "topology.replicas")
            _require_positive(self.tp, "topology.tp")
        else:
            for name, val in (
                ("topology.prefill_replicas", self.prefill_replicas),
                ("topology.prefill_tp", self.prefill_tp),
                ("topology.decode_replicas", self.decode_replicas),
                ("topology.decode_tp", self.decode_tp),
            ):
                if val is None:
                    raise ValueError(f"{name} is required when mode='disagg'")
                _require_positive(val, name)
            if self.pd_transport is None:
                raise ValueError(
                    "topology.pd_transport is required when mode='disagg' "
                    "(one of 'rdma' | 'tcp')"
                )
            _require_in(self.pd_transport, ALLOWED_PD_TRANSPORT, "topology.pd_transport")

    def total_replicas(self) -> int:
        if self.mode == "agg":
            return self.replicas or 0
        return (self.prefill_replicas or 0) + (self.decode_replicas or 0)


@dataclass
class Features:
    """Opt-in feature flags. Defaults conservative.

    Phase B additions (tiered_cache, flow_control, wide_ep,
    inference_objective, model_rewrite, autoscaler, serving_pattern) are
    signals to the agent (SKILL Phase 2.5) about which docs to read. The
    script does NOT auto-add plugins/values for these — the agent's
    recommendation does. Flags are in input_hash so the same toggle
    flips reproducibly across runs.
    """
    enable_latency_predictor: bool = False
    enable_precise_prefix_cache: bool = False
    enable_tiered_cache: bool = False
    enable_flow_control: bool = False
    enable_wide_ep: bool = False
    enable_inference_objective: bool = False
    enable_model_rewrite: bool = False
    autoscaler: str | None = None      # None | "wva" | "hpa"
    serving_pattern: str = "sync"      # "sync" | "batch" | "async"
    autotune_supported: bool = True

    def validate(self) -> None:
        if self.autoscaler not in ALLOWED_AUTOSCALER:
            raise ValueError(
                f"features.autoscaler must be one of {sorted(s for s in ALLOWED_AUTOSCALER if s)} or null; "
                f"got {self.autoscaler!r}"
            )
        _require_in(self.serving_pattern, ALLOWED_SERVING_PATTERN, "features.serving_pattern")


@dataclass
class Runtime:
    """Runtime values normally supplied by autoTune. Only used when
    features.autotune_supported is False."""
    block_size_tokens: int | None = None
    lru_capacity_per_server: int | None = None

    def validate(self) -> None:
        if self.block_size_tokens is not None:
            _require_positive(self.block_size_tokens, "runtime.block_size_tokens")
        if self.lru_capacity_per_server is not None:
            _require_positive(self.lru_capacity_per_server, "runtime.lru_capacity_per_server")


@dataclass
class WorkloadTraits:
    """Yes/no signals from the workload-knowledge checklist."""
    multi_turn: bool = False
    lora: bool = False
    heterogeneous_context: bool = False
    epp_ha: bool = False
    multimodal: bool = False


@dataclass
class Correctness:
    """Values that must match the deployed vLLM exactly when their feature is on."""
    vllm_block_size: int | None = None
    vllm_hash_seed: str | None = None


@dataclass
class Context:
    namespace: str = "default"
    release_name: str = "llm-d"
    deploy_mode: str = "standalone"           # standalone | gateway
    gateway_provider: str | None = None       # istio | kgateway | agentgateway | gke-l7-* | null

    # Will the bundle apply also deploy modelserver pods, or are the model
    # servers pre-existing? Set false when Phase 2 Q0 = "configure for
    # existing pods". Drives:
    #   - Phase 3 schedulability audit: skips density math (informational only)
    #   - HF Secret scaffold: rendered only when a fresh modelserver deploy
    #     is planned AND the user picked the scaffold option at Q0.5
    modelserver_deploy_planned: bool = True

    # HF token Secret coordinates. hf_secret_name = the name the bench Job +
    # any modelserver overlay should reference. Set from Phase 2 Q0.5:
    #   - User picked an existing secret  → that name (e.g. "hf-token-secret")
    #   - User picked "scaffold new"      → "llm-d-hf-token" (autoconfig's default)
    #   - User picked "skip, public model" → null
    # hf_secret_exists = did Phase 1 find a Secret literally matching
    # hf_secret_name in the target namespace? Prevents render_bundle from
    # clobbering an existing real token with an empty scaffold on re-apply.
    hf_secret_name: str | None = None
    hf_secret_exists: bool = False

    # Optional override for the benchmark Job's tokenizer download. Falls back
    # to the served model id when null. Set this when the model isn't on HF
    # (proprietary / GCS / S3 weights) but a public HF tokenizer can be used
    # (e.g. "meta-llama/Llama-3.1-8B-Instruct" for a proprietary Llama variant).
    bench_tokenizer_override: str | None = None

    def validate(self) -> None:
        _require_in(self.deploy_mode, ALLOWED_DEPLOY_MODE, "context.deploy_mode")
        if self.gateway_provider not in ALLOWED_GATEWAY_PROVIDER:
            raise ValueError(
                f"context.gateway_provider must be one of "
                f"{sorted(s for s in ALLOWED_GATEWAY_PROVIDER if s)} or null; "
                f"got {self.gateway_provider!r}"
            )
        if self.deploy_mode == "gateway" and self.gateway_provider is None:
            raise ValueError(
                "context.gateway_provider is required when context.deploy_mode='gateway'"
            )


@dataclass
class Recommendation:
    """The agent's doc-driven recommendation, emitted from SKILL Phase 2.

    All fields default to empty. The agent populates them by reading upstream
    docs (per feature_docs.yaml) and synthesizing a configuration.

    When a field is empty, the script falls back to a canonical default:
    - agg topology: _DEFAULT_AGG_PLUGINS + _DEFAULT_AGG_WEIGHTS
    - PD topology: _DEFAULT_PD_PLUGINS + _DEFAULT_PD_PROFILE_WEIGHTS
    """
    # Plugins for the EndpointPickerConfig `plugins[]` array. Each entry is an
    # object with a required "type", an optional "name" (for a named instance,
    # e.g. two affinity filters at different thresholds), and an optional
    # "parameters" block (overrides what the script would otherwise derive):
    #   [{"type": "queue-scorer"},
    #    {"type": "prefix-cache-affinity-filter", "name": "strict-affinity-filter"},
    #    {"type": "precise-prefix-cache-scorer", "parameters": {...}}]
    plugins: list = field(default_factory=list)

    # Per-plugin weights for the schedulingProfiles. Agent populates from
    # the relevant guide's values.yaml. For PD, the agent should populate
    # `scheduling_profiles` instead (two profiles, each with their own
    # weight set) — this field is the agg-mode shortcut.
    weights: dict[str, int] = field(default_factory=dict)

    # Full schedulingProfiles[] structure. Use for PD (two profiles) or
    # any non-default profile shape. For agg with default single profile,
    # leave empty; script builds it from `plugins` + `weights`.
    scheduling_profiles: list[dict] = field(default_factory=list)

    # URLs the agent read to build this recommendation. Cited in
    # output.rationale[] for the audit trail.
    cited_sources: list[str] = field(default_factory=list)

    # Agent's 1-3 sentence summary of the recommendation. Includes
    # quoted material from the cited docs. Surfaced in output.rationale[].
    summary: str = ""

    # Parameter blocks pulled from the "parameters" field of `plugins` entries,
    # keyed by plugin identifier. Override the script's derived params. Usually
    # empty — the script derives parameters.
    inline_parameters: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Reduce each plugins entry to the identifier the rest of the script
        # keys on ("name" if present, else "type") and capture its parameters.
        normalized: list[str] = []
        for entry in self.plugins:
            if not isinstance(entry, dict) or not entry.get("type"):
                raise ValueError(
                    "recommendation.plugins entries must be objects with a "
                    f'"type" key, e.g. {{"type": "queue-scorer"}}; got {entry!r}.'
                )
            identifier = entry.get("name") or entry["type"]
            normalized.append(identifier)
            params = entry.get("parameters")
            if isinstance(params, dict) and params:
                self.inline_parameters.setdefault(identifier, {}).update(params)
        self.plugins = normalized


@dataclass
class Input:
    model: str
    topology: Topology
    workload: Workload = field(default_factory=Workload)
    slo: SLO = field(default_factory=SLO)
    features: Features = field(default_factory=Features)
    workload_traits: WorkloadTraits = field(default_factory=WorkloadTraits)
    correctness: Correctness = field(default_factory=Correctness)
    runtime: Runtime = field(default_factory=Runtime)
    context: Context = field(default_factory=Context)
    recommendation: Recommendation = field(default_factory=Recommendation)
    model_context_length: int | None = None
    bench_harness: str = "guidellm"
    version: str = VERSION

    def validate(self) -> None:
        if not self.model:
            raise ValueError("model is required")
        self.topology.validate()
        self.workload.validate()
        self.slo.validate()
        self.runtime.validate()
        self.features.validate()
        self.context.validate()
        if self.model_context_length is not None:
            _require_positive(self.model_context_length, "model_context_length")
        if self.bench_harness not in ("guidellm", "inference-perf"):
            raise ValueError(
                f"bench_harness must be 'guidellm' or 'inference-perf'; got {self.bench_harness!r}"
            )


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


@dataclass
class Parameter:
    """One derived parameter on one plugin, with evidence tier + citation."""
    name: str
    value: object
    tier: str           # T1 (math) | T2 (correctness) | T3 (citation) | T4 (principle)
    rationale: str      # explanation including formula / citation / fallback reason


@dataclass
class Output:
    input_hash: str
    decisions: dict
    rationale: list[str]
    parameters: list[dict]
    unresolved_questions: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    version: str = VERSION


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_positive(n: int, name: str) -> None:
    if not isinstance(n, int) or n <= 0:
        raise ValueError(f"{name} must be a positive integer; got {n!r}")


def _require_in(v: str, allowed: set[str], name: str) -> None:
    if v not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}; got {v!r}")


def parse_input(raw_json: str) -> Input:
    """Parse + validate input JSON; returns an Input or raises ValueError."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"input is not valid JSON: {e}") from e

    try:
        inp = Input(
            model=data["model"],
            topology=Topology(**data["topology"]),
            workload=Workload(**data.get("workload", {})),
            slo=SLO(**data.get("slo", {})),
            features=Features(**data.get("features", {})),
            workload_traits=WorkloadTraits(**data.get("workload_traits", {})),
            correctness=Correctness(**data.get("correctness", {})),
            runtime=Runtime(**data.get("runtime", {})),
            context=Context(**data.get("context", {})),
            recommendation=Recommendation(**data.get("recommendation", {})),
            model_context_length=data.get("model_context_length"),
            bench_harness=data.get("bench_harness", "guidellm"),
            version=data.get("version", VERSION),
        )
    except (KeyError, TypeError) as e:
        raise ValueError(f"input does not match schema: {e}") from e

    inp.validate()
    return inp


# ---------------------------------------------------------------------------
# Workload signal labeling (used only by benchmark.py for rate hinting)
# ---------------------------------------------------------------------------


def classify_workload(wl: Workload, slo: SLO) -> str:
    """Tag the workload with a signal label for benchmark.py rate hinting.

    Not used for plugin/weight selection — the agent makes those decisions
    from doc reads at SKILL Phase 2.
    """
    if wl.prefix_share in ("high", "medium"):
        return "high-prefix-share"
    if slo.ttft_ms is not None and slo.ttft_ms < 300:
        return "latency-tight"
    return "balanced-conversational"


# ---------------------------------------------------------------------------
# Plugin / weight / profile resolution
#
# Agent-supplied recommendation > canonical default per topology.
# ---------------------------------------------------------------------------


def _has_slo(inp: Input) -> bool:
    return bool(inp.slo.ttft_ms or inp.slo.tpot_ms or inp.slo.request_latency_ms)


def resolve_plugins(inp: Input) -> list[str]:
    """Return the plugin list to emit in `plugins[]`.

    Source of truth: `inp.recommendation.plugins` (agent-supplied).
    Fallback: canonical default per (topology, features.enable_latency_predictor, SLO).
    """
    if inp.recommendation.plugins:
        return list(inp.recommendation.plugins)
    if inp.topology.mode == "disagg":
        plugins = list(_DEFAULT_PD_PLUGINS)
        if inp.features.enable_latency_predictor:
            # Producer is PD-aware via endpointRoleLabel; appending preserves
            # the canonical PD profile structure. SLO-aware admitter NOT added
            # under PD (no published canonical for that combo).
            plugins.append("predicted-latency-producer")
        return plugins
    # agg
    if inp.features.enable_latency_predictor:
        if _has_slo(inp):
            return list(_DEFAULT_LATENCY_PREDICTOR_SLO_PLUGINS)
        return list(_DEFAULT_LATENCY_PREDICTOR_BASELINE_PLUGINS)
    return list(_DEFAULT_AGG_PLUGINS)


def resolve_weights(inp: Input) -> dict[str, int]:
    """Return per-plugin weights for the single-profile (agg) case.

    Source of truth: `inp.recommendation.weights`.
    Fallback: canonical default. Empty if PD (PD uses scheduling_profiles)
    or if latency-predictor SLO profile (unweighted refs).
    """
    if inp.recommendation.weights:
        return dict(inp.recommendation.weights)
    if inp.topology.mode == "disagg":
        return {}
    if inp.features.enable_latency_predictor and _has_slo(inp):
        # SLO profile is fully unweighted per canonical.
        return {}
    return dict(_DEFAULT_AGG_WEIGHTS)


def resolve_scheduling_profiles(
    inp: Input,
    plugins: list[str],
    weights: dict[str, int],
) -> list[dict]:
    """Return the `schedulingProfiles[]` list to emit.

    Source of truth: `inp.recommendation.scheduling_profiles` (agent-supplied).
    Fallback for PD: canonical two-profile structure.
    Fallback for agg + latency-predictor SLO: canonical unweighted profile.
    Fallback for agg: single "default" profile from `plugins` + `weights`.
    """
    if inp.recommendation.scheduling_profiles:
        return list(inp.recommendation.scheduling_profiles)
    if inp.topology.mode == "disagg":
        return [
            {"name": "prefill", "plugins": _build_pd_profile_plugins(plugins, "prefill")},
            {"name": "decode",  "plugins": _build_pd_profile_plugins(plugins, "decode")},
        ]
    if inp.features.enable_latency_predictor and _has_slo(inp):
        return [{
            "name": "default",
            "plugins": [
                {"pluginRef": ref}
                for ref in _DEFAULT_LATENCY_PREDICTOR_SLO_PROFILE_REFS
                if ref in plugins
            ],
        }]
    return [{"name": "default", "plugins": _build_default_profile_plugins(plugins, weights)}]


def _build_default_profile_plugins(
    plugins: list[str],
    weights: dict[str, int],
) -> list[dict]:
    """Build a single-profile plugins[] from a flat plugin list + weight map.

    Picker / handler / unweighted plugins (filters, profile handlers) appear
    as `{pluginRef: <name>}` without a weight.
    """
    entries: list[dict] = []
    for t in plugins:
        if t in _PLUGINS_NOT_IN_PROFILE:
            continue
        w = weights.get(t)
        if w is not None:
            entries.append({"pluginRef": t, "weight": w})
        else:
            entries.append({"pluginRef": t})
    return entries


def _build_pd_profile_plugins(plugins: list[str], profile_name: str) -> list[dict]:
    """Build one PD scheduling profile's plugins[] list.

    Mirrors pd-disaggregation.values.yaml — each profile gets its role filter
    + the canonical weighted scorers for that profile + max-score-picker.
    """
    profile_weights = _DEFAULT_PD_PROFILE_WEIGHTS[profile_name]
    role_filter = _DEFAULT_PD_PROFILE_FILTERS[profile_name]
    other_role_filter = (
        _DEFAULT_PD_PROFILE_FILTERS["decode"]
        if profile_name == "prefill"
        else _DEFAULT_PD_PROFILE_FILTERS["prefill"]
    )

    entries: list[dict] = []
    for t in plugins:
        if t in _PLUGINS_NOT_IN_PROFILE or t == other_role_filter:
            continue
        if t == role_filter:
            entries.append({"pluginRef": t})
            continue
        w = profile_weights.get(t)
        if w is not None:
            entries.append({"pluginRef": t, "weight": w})
        elif t == "max-score-picker":
            entries.append({"pluginRef": t})
    return entries


def _build_plugin_entries(
    plugins: list[str],
    params_by_plugin: dict[str, dict],
) -> list[dict]:
    """Convert plugin type list + per-plugin params → top-level plugins[].

    Layering for parameters:
      1. _FIXED_PLUGIN_PARAMETERS (canonical fixed defaults, e.g. metrics-data-source)
      2. _NAMED_PLUGIN_INSTANCES parameters (named-instance defaults)
      3. params_by_plugin (derived params from inputs) — wins
    """
    out: list[dict] = []
    for t in plugins:
        spec = _NAMED_PLUGIN_INSTANCES.get(t)
        if spec is not None:
            entry: dict = {"type": spec["type"], "name": t}
            merged: dict = {}
            merged.update(_FIXED_PLUGIN_PARAMETERS.get(spec["type"], {}))
            merged.update(spec["parameters"])
            merged.update(params_by_plugin.get(t, {}))
            if merged:
                entry["parameters"] = merged
        else:
            entry = {"type": t}
            merged = {}
            merged.update(_FIXED_PLUGIN_PARAMETERS.get(t, {}))
            merged.update(params_by_plugin.get(t, {}))
            if merged:
                entry["parameters"] = merged
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Per-plugin parameter derivations (deterministic computations from inputs).
# Each carries a comment citing either its upstream source or "our derivation."
# ---------------------------------------------------------------------------


def derive_parameters(
    plugins: list[str],
    inp: Input,
) -> tuple[dict[str, dict], list[Parameter], list[dict]]:
    """Compute per-plugin parameter blocks.

    Only generates params for plugins that are actually present in `plugins`.
    Returns (params_by_plugin, inspect_records, unresolved_questions).
    """
    params: dict[str, dict] = {p: {} for p in plugins}
    inspect: list[Parameter] = []
    unresolved: list[dict] = []

    # disagg-profile-handler must reference always-disagg-pd-decider by name.
    # Source (verbatim): pd-disaggregation.values.yaml.
    if "disagg-profile-handler" in plugins:
        params["disagg-profile-handler"]["deciderPluginName"] = "always-disagg-pd-decider"
        inspect.append(Parameter(
            name="disagg-profile-handler.deciderPluginName",
            value="always-disagg-pd-decider",
            tier="T2",
            rationale=(
                "must reference always-disagg-pd-decider for every request to "
                "run both PD stages. Verbatim from pd-disaggregation.values.yaml."
            ),
        ))

    # predicted-latency-producer.endpointRoleLabel — required for PD so the
    # producer can identify prefill vs decode pods and neutralize TPOT for
    # prefill (producer README §"Disaggregated Serving").
    if "predicted-latency-producer" in plugins and inp.topology.mode == "disagg":
        params["predicted-latency-producer"]["endpointRoleLabel"] = "llm-d.ai/role"
        inspect.append(Parameter(
            name="predicted-latency-producer.endpointRoleLabel",
            value="llm-d.ai/role",
            tier="T2",
            rationale=(
                "auto-set under PD so the producer identifies prefill pods and "
                "neutralizes their TPOT predictions. Source: predicted-latency-"
                "producer README §'Disaggregated Serving'. Key matches the "
                "label set by pd-disaggregation modelserver kustomization."
            ),
        ))

    # latency-scorer composite-fallback weights — carry the canonical agg
    # plugin weights through to the composite fallback (latency-scorer
    # subsumes queue/kv/prefix when active).
    if "latency-scorer" in plugins:
        weights = resolve_weights(inp)
        for src_key, dst_key, label in (
            ("kv-cache-utilization-scorer", "compositeKVWeight", "kv-cache"),
            ("queue-scorer", "compositeQueueWeight", "queue"),
            ("prefix-cache-scorer", "compositePrefixWeight", "prefix-cache"),
        ):
            if src_key in weights:
                params["latency-scorer"][dst_key] = float(weights[src_key])
                inspect.append(Parameter(
                    name=f"latency-scorer.{dst_key}",
                    value=float(weights[src_key]),
                    tier="T4",
                    rationale=(
                        f"workload {label} weight ({weights[src_key]}) carried into "
                        "latency-scorer's composite fallback (used when latency "
                        "predictions unavailable)."
                    ),
                ))

    # latency-scorer.ttftWeight / tpotWeight from SLA + OSL.
    # Our derivation (NOT upstream-prescribed). The latency-scorer README's
    # Config table lists `ttftWeight: 0.8 default`, `tpotWeight: 0.2 default`
    # with no formula. We derive these from the user's SLA on the principle
    # that ttftWeight should reflect TTFT's share of total predicted latency:
    #   ttft contributes once (≈1 token of latency)
    #   tpot contributes (osl-1) times (per generated token after first)
    # → ttftWeight = ttft / (ttft + tpot*(osl-1)), clamped [0.1, 0.9].
    # When inputs missing, fall back to upstream defaults (0.8 / 0.2). Plugin docs:
    # https://github.com/llm-d/llm-d-router/blob/main/pkg/epp/framework/plugins/scheduling/scorer/latency/README.md
    if "latency-scorer" in plugins:
        if inp.slo.ttft_ms and inp.slo.tpot_ms and inp.workload.osl:
            ttft_contrib = float(inp.slo.ttft_ms)
            tpot_contrib = float(inp.slo.tpot_ms) * (inp.workload.osl - 1)
            ttft_w_raw = ttft_contrib / (ttft_contrib + tpot_contrib)
            ttft_w = max(0.1, min(0.9, round(ttft_w_raw, 2)))
            tpot_w = round(1.0 - ttft_w, 2)
            params["latency-scorer"]["ttftWeight"] = ttft_w
            params["latency-scorer"]["tpotWeight"] = tpot_w
            inspect.append(Parameter(
                name="latency-scorer.ttftWeight",
                value=ttft_w,
                tier="T1",
                rationale=(
                    f"computed from SLA TTFT {inp.slo.ttft_ms}ms, TPOT "
                    f"{inp.slo.tpot_ms}ms, OSL {inp.workload.osl}: "
                    f"ttftWeight = {ttft_contrib:.0f} / "
                    f"({ttft_contrib:.0f} + {tpot_contrib:.0f}) = "
                    f"{ttft_w_raw:.3f}, clamped to [0.1, 0.9]. "
                    "(Derivation is ours, not upstream-prescribed; the "
                    "latency-scorer README's default is 0.8.)"
                ),
            ))
            inspect.append(Parameter(
                name="latency-scorer.tpotWeight",
                value=tpot_w,
                tier="T1",
                rationale=f"1 - ttftWeight ({ttft_w}) = {tpot_w}",
            ))
        else:
            # Fall back to upstream defaults
            inspect.append(Parameter(
                name="latency-scorer.ttftWeight",
                value=0.8,
                tier="T3",
                rationale=(
                    "upstream default 0.8 — SLA inputs missing "
                    f"(ttft={inp.slo.ttft_ms}, tpot={inp.slo.tpot_ms}, "
                    f"osl={inp.workload.osl})"
                ),
            ))
            inspect.append(Parameter(
                name="latency-scorer.tpotWeight",
                value=0.2,
                tier="T3",
                rationale="upstream default 0.2 — same reason",
            ))

    # prefix-cache-scorer.autoTune + fallback runtime values.
    # Source: prefix-cache-scorer README §"autoTune".
    if "prefix-cache-scorer" in plugins:
        if inp.features.autotune_supported:
            params["prefix-cache-scorer"]["autoTune"] = True
            inspect.append(Parameter(
                name="prefix-cache-scorer.autoTune",
                value=True,
                tier="T3",
                rationale=(
                    "plugin default. Reads vllm:cache_config_info per-pod to size "
                    "LRUCapacityPerServer (num_gpu_blocks) and BlockSizeTokens "
                    "(block_size). Verify the model server exports the metric."
                ),
            ))
        else:
            params["prefix-cache-scorer"]["autoTune"] = False
            inspect.append(Parameter(
                name="prefix-cache-scorer.autoTune",
                value=False,
                tier="T3",
                rationale=(
                    "model server doesn't export vllm:cache_config_info — autoTune "
                    "disabled; LRU + blockSize must be supplied explicitly."
                ),
            ))
            if inp.runtime.block_size_tokens:
                params["prefix-cache-scorer"]["blockSizeTokens"] = inp.runtime.block_size_tokens
                inspect.append(Parameter(
                    name="prefix-cache-scorer.blockSizeTokens",
                    value=inp.runtime.block_size_tokens,
                    tier="T2",
                    rationale="user-supplied (must match vLLM's --block-size)",
                ))
            if inp.runtime.lru_capacity_per_server:
                params["prefix-cache-scorer"]["lruCapacityPerServer"] = inp.runtime.lru_capacity_per_server
                inspect.append(Parameter(
                    name="prefix-cache-scorer.lruCapacityPerServer",
                    value=inp.runtime.lru_capacity_per_server,
                    tier="T1",
                    rationale=(
                        "user-supplied; read from vLLM startup log line "
                        "'Total number of GPU blocks: N' or computed from HBM."
                    ),
                ))
            if not (inp.runtime.block_size_tokens or inp.runtime.lru_capacity_per_server):
                # Surface a synthetic "fallback" parameter so the agent (and any
                # downstream consumer) sees that we left autotune off with no
                # runtime values to fall back on.
                inspect.append(Parameter(
                    name="prefix-cache-scorer.fallback",
                    value=(
                        "blockSizeTokens and lruCapacityPerServer both unset; "
                        "plugin will use upstream defaults (blockSizeTokens=16, "
                        "lruCapacityPerServer=4096)"
                    ),
                    tier="T3",
                    rationale=(
                        "autoTune disabled and no runtime values supplied — the "
                        "plugin will fall back to its hardcoded defaults, which "
                        "may not match vLLM's actual block size / cache capacity."
                    ),
                ))

    # prefix-cache-scorer.maxPrefixTokensToMatch — T1 from model context length.
    # When model_context_length is missing, emit a synthetic Parameter entry
    # with "fallback" tier instead of silently dropping the param, so the agent
    # sees the gap and can add the field or surface it to the user.
    if "prefix-cache-scorer" in plugins:
        if inp.model_context_length:
            params["prefix-cache-scorer"]["maxPrefixTokensToMatch"] = inp.model_context_length
            inspect.append(Parameter(
                name="prefix-cache-scorer.maxPrefixTokensToMatch",
                value=inp.model_context_length,
                tier="T1",
                rationale=(
                    f"set to model_context_length={inp.model_context_length}. "
                    "Plugin divides by autoTuned block_size at request time."
                ),
            ))
        else:
            inspect.append(Parameter(
                name="prefix-cache-scorer.maxPrefixTokensToMatch",
                value=None,
                tier="T3",
                rationale=(
                    "OMITTED — input.model_context_length is null. The plugin "
                    "falls back to its built-in default, which may not match "
                    "the model's actual max position embeddings. Fetch the "
                    "value from HF config.json (`max_position_embeddings` key) "
                    "or the modelserver's `--max-model-len` arg, add to the "
                    "input JSON, and regenerate."
                ),
            ))

    # prefix-cache-affinity-filter.maxTTFTPenaltyMs.
    # Our derivation (NOT upstream-prescribed): cap the TTFT penalty at 2× the
    # SLA TTFT so stickiness can't violate it by more than 2×, then clip at the
    # upstream default of 5000. When ttft_ms is missing, fall through to 5000.
    # Plugin docs:
    # https://github.com/llm-d/llm-d-router/blob/main/pkg/epp/framework/plugins/scheduling/filter/prefixcacheaffinity/README.md
    if "prefix-cache-affinity-filter" in plugins:
        if inp.slo.ttft_ms:
            penalty = min(5000, inp.slo.ttft_ms * 2)
            params["prefix-cache-affinity-filter"]["maxTTFTPenaltyMs"] = penalty
            inspect.append(Parameter(
                name="prefix-cache-affinity-filter.maxTTFTPenaltyMs",
                value=penalty,
                tier="T1",
                rationale=(
                    f"min(5000, SLA TTFT {inp.slo.ttft_ms}ms × 2). Stickiness "
                    "shouldn't violate SLA by >2×. (Our derivation; upstream "
                    "default is 5000.)"
                ),
            ))
        else:
            inspect.append(Parameter(
                name="prefix-cache-affinity-filter.maxTTFTPenaltyMs",
                value=5000,
                tier="T3",
                rationale="upstream default 5000ms — no SLA TTFT to derive from",
            ))

    # no-hit-lru-scorer.lruSize.
    # Our derivation (NOT upstream-prescribed): size to 2× replica count
    # (memory-efficient for small fleets), with a floor of 16 for headroom.
    # Upstream default is a flat 1024 — generous, sized for any cluster. Plugin docs:
    # https://github.com/llm-d/llm-d-router/blob/main/pkg/epp/framework/plugins/scheduling/scorer/nohitlru/README.md
    # NOTE: prefixPluginName / prefixPluginType are intentionally NOT emitted:
    # per no_hit_lru.go:107-118 these fields are stored on the struct but
    # never read at runtime; both basic + precise prefix scorers write to the
    # same attribute key that no-hit-lru reads directly without plugin lookup.
    if "no-hit-lru-scorer" in plugins:
        total = inp.topology.total_replicas()
        lru_size = max(16, total * 2)
        params["no-hit-lru-scorer"]["lruSize"] = lru_size
        inspect.append(Parameter(
            name="no-hit-lru-scorer.lruSize",
            value=lru_size,
            tier="T4",
            rationale=(
                f"max(16, replicas {total} × 2 = {total * 2}). LRU must track "
                "all pods with headroom. (Our derivation; upstream default = 1024.)"
            ),
        ))

    # precise-prefix-cache-scorer correctness inputs (T2)
    if "precise-prefix-cache-scorer" in plugins:
        if inp.correctness.vllm_block_size and inp.correctness.vllm_hash_seed:
            params["precise-prefix-cache-scorer"].setdefault("tokenProcessorConfig", {})
            params["precise-prefix-cache-scorer"]["tokenProcessorConfig"]["blockSize"] = inp.correctness.vllm_block_size
            params["precise-prefix-cache-scorer"]["tokenProcessorConfig"]["hashSeed"] = inp.correctness.vllm_hash_seed
            params["precise-prefix-cache-scorer"].setdefault("indexerConfig", {})
            params["precise-prefix-cache-scorer"]["indexerConfig"].setdefault(
                "tokenizersPoolConfig", {}
            )
            params["precise-prefix-cache-scorer"]["indexerConfig"]["tokenizersPoolConfig"]["modelName"] = inp.model
            inspect.append(Parameter(
                name="precise-prefix-cache-scorer.tokenProcessorConfig.blockSize",
                value=inp.correctness.vllm_block_size,
                tier="T2",
                rationale="must match vLLM's --block-size (correctness)",
            ))
            inspect.append(Parameter(
                name="precise-prefix-cache-scorer.tokenProcessorConfig.hashSeed",
                value=inp.correctness.vllm_hash_seed,
                tier="T2",
                rationale="must match vLLM's PYTHONHASHSEED (correctness)",
            ))
        else:
            unresolved.append({
                "parameter": "precise-prefix-cache-scorer.tokenProcessorConfig.blockSize+hashSeed",
                "required": True,
                "question": (
                    "Precise prefix cache requires vLLM's --block-size and "
                    "PYTHONHASHSEED for correctness. Mismatch silently breaks "
                    "the cache. Please provide both."
                ),
                "verification": (
                    "kubectl get pod <vllm-pod> -o yaml | grep -E "
                    "'PYTHONHASHSEED|block-size'"
                ),
            })

    return params, inspect, unresolved


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------


def _input_to_dict(inp: Input) -> dict:
    """Stable dict form of Input for hashing."""
    return {
        "version": inp.version,
        "model": inp.model,
        "model_context_length": inp.model_context_length,
        "bench_harness": inp.bench_harness,
        "topology": dataclasses.asdict(inp.topology),
        "workload": dataclasses.asdict(inp.workload),
        "slo": dataclasses.asdict(inp.slo),
        "features": dataclasses.asdict(inp.features),
        "workload_traits": dataclasses.asdict(inp.workload_traits),
        "correctness": dataclasses.asdict(inp.correctness),
        "runtime": dataclasses.asdict(inp.runtime),
        "context": dataclasses.asdict(inp.context),
        "recommendation": _recommendation_for_hash(inp.recommendation),
    }


def _recommendation_for_hash(rec: Recommendation) -> dict:
    """Recommendation as a dict for input hashing. Omits inline_parameters when
    empty — it's derived from the plugins entries, so empty adds nothing."""
    d = dataclasses.asdict(rec)
    if not d.get("inline_parameters"):
        d.pop("inline_parameters", None)
    return d


def build_output(inp: Input) -> Output:
    """Build the full Output (decisions + rationale + parameters + warnings)
    from the validated Input."""
    plugins = resolve_plugins(inp)
    weights = resolve_weights(inp)
    params_by_plugin, inspect, unresolved = derive_parameters(plugins, inp)

    # Inline parameters from the recommendation override derived ones.
    for name, p in inp.recommendation.inline_parameters.items():
        params_by_plugin.setdefault(name, {}).update(p)

    plugin_entries = _build_plugin_entries(plugins, params_by_plugin)
    scheduling_profiles = resolve_scheduling_profiles(inp, plugins, weights)

    epp_config = {
        "apiVersion": "llm-d.ai/v1alpha1",
        "kind": "EndpointPickerConfig",
        "plugins": plugin_entries,
        "schedulingProfiles": scheduling_profiles,
    }

    # Topology summary
    if inp.topology.mode == "disagg":
        topology_summary = (
            f"Topology: disagg (PD), "
            f"prefill={inp.topology.prefill_replicas}×TP={inp.topology.prefill_tp}, "
            f"decode={inp.topology.decode_replicas}×TP={inp.topology.decode_tp}, "
            f"transport={inp.topology.pd_transport}"
        )
    else:
        topology_summary = (
            f"Topology: agg, replicas={inp.topology.replicas}, tp={inp.topology.tp}"
        )

    rationale: list[str] = [
        topology_summary,
        f"Plugin set ({len(plugins)} plugins): {', '.join(plugins)}",
        "Chart: llm-d-router (standalone variant; matches optimized-baseline guide)",
    ]
    if inp.recommendation.summary:
        rationale.append(f"Agent recommendation: {inp.recommendation.summary}")
    if inp.recommendation.cited_sources:
        rationale.append("Cited sources:")
        for src in inp.recommendation.cited_sources:
            rationale.append(f"  - {src}")
    for p in inspect:
        rationale.append(f"{p.name} = {p.value} — {p.rationale}")

    # Warnings
    warnings: list[str] = []
    if inp.topology.mode == "disagg" and inp.topology.pd_transport == "tcp":
        warnings.append(
            "PD transport: TCP fallback. NIXL uses TCP because the cluster "
            "doesn't expose RDMA NICs (no DPv2 + multi-networking + rdma/ib). "
            "PD plugins are wired correctly, but inter-pod KV transfer over "
            "TCP is typically slower than aggregated serving on the same "
            "hardware. Benchmark BOTH agg and PD; PD often loses to agg "
            "without RDMA. The PD guide's headline numbers were collected on "
            "Infiniband CKS."
        )

    if inp.features.enable_latency_predictor:
        warnings.append(
            "latency-predictor: deploy via the chart toggle "
            "`router.latencyPredictor.enabled=true` — this adds "
            "2 sidecar containers per EPP pod at chart defaults (1 training-"
            "server + 1 prediction-server; predictionServers.count is "
            "configurable). Resource asks: ~8Gi memory requested / 16Gi "
            "limit, ~30Gi emptyDir, ~10 CPU requested. Startup +30-60s. "
            "Source: llm-d-router/config/charts/routerlib/values.yaml."
        )
        warnings.append(
            "latency-predictor assumes a homogeneous InferencePool (same "
            "GPU/model/serving config across pods). Mixed pools give bad "
            "predictions."
        )
        warnings.append(
            "latency-predictor SLO mode uses streamingMode=true on the producer. "
            "If non-streaming clients hit this stack they won't contribute to "
            "training samples — set streamingMode=false if your clients don't "
            "stream. Source: predicted-latency-slo.values.yaml."
        )

    if not (inp.slo.ttft_ms or inp.slo.tpot_ms or inp.slo.request_latency_ms):
        warnings.append(
            "No SLA targets provided — derived parameters fall back to plugin "
            "defaults."
        )

    # Surface the prefix-cache + missing-context-length case as a warning, not
    # just a parameter rationale, because Phase 5 routes warnings[] to the
    # most prominent part of the user-facing recap. First-time Gemini test:
    # the agent fetched the value in Phase 1 but forgot to thread it into the
    # input JSON. Silent skip in the EPP config; user didn't notice until
    # checking the rendered YAML.
    has_prefix_cache_scorer = any(
        p in plugins for p in ("prefix-cache-scorer", "precise-prefix-cache-scorer")
    )
    if has_prefix_cache_scorer and not inp.model_context_length:
        warnings.append(
            "prefix-cache-scorer is included but input.model_context_length is "
            "null — the script DID NOT emit maxPrefixTokensToMatch in the "
            "EPP config (the plugin's built-in default applies). Phase 1 "
            "fetches max_position_embeddings from HF config.json for any "
            "public model; re-check that the value made it into the input "
            "JSON, then regenerate."
        )

    # Phase B feature flag advisories — these don't change rendered EPP config
    # today; they signal the agent which docs to read in SKILL Phase 2.5 and
    # tell the user the autoscaler/tiered-cache/etc. side still needs hand-wiring.
    if inp.features.autoscaler == "wva":
        warnings.append(
            "autoscaler=wva: VariantAutoscaling CRs + WVA operator install are NOT "
            "rendered by this script. See guides.workload_autoscaling_wva in "
            "feature_docs.yaml for the helmfile to apply alongside the EPP config."
        )
    if inp.features.autoscaler == "hpa":
        warnings.append(
            "autoscaler=hpa: HPA YAML driven by EPP metrics is NOT rendered by this "
            "script. See guides.workload_autoscaling_hpa in feature_docs.yaml for the "
            "HPA manifest pattern."
        )
    if inp.features.enable_tiered_cache:
        warnings.append(
            "enable_tiered_cache: the script doesn't render the tiered-cache "
            "modelservice overlay. See guides.tiered_prefix_cache in feature_docs.yaml "
            "for the kustomize overlay to apply on the model server side."
        )
    if inp.features.enable_wide_ep:
        warnings.append(
            "enable_wide_ep: LeaderWorkerSet wide-EP topology is a modelserver-side "
            "deploy pattern (not EPP config). See guides.wide_ep_lws."
        )
    if inp.features.enable_flow_control:
        warnings.append(
            "enable_flow_control: the agent should add flow-control plugins "
            "(ordering, fairness, usage-limits, saturation-detector) via "
            "recommendation.plugins. See guides.flow_control."
        )
    if inp.features.serving_pattern == "batch":
        warnings.append(
            "serving_pattern=batch: batch-gateway is a separate gateway pattern. "
            "See guides.batch_gateway in feature_docs.yaml."
        )
    if inp.features.serving_pattern == "async":
        warnings.append(
            "serving_pattern=async: see guides.asynchronous_processing in feature_docs.yaml."
        )

    # enable_precise_prefix_cache is an agent signal; the script doesn't add
    # the plugin itself (agent populates recommendation.plugins). Catch the
    # mismatch where the flag is on but the agent forgot to include the
    # scorer — surface the gap instead of failing silently.
    if inp.features.enable_precise_prefix_cache:
        if "precise-prefix-cache-scorer" not in plugins:
            warnings.append(
                "enable_precise_prefix_cache=true but recommendation.plugins doesn't "
                "include precise-prefix-cache-scorer. Phase 2.5 should have added it "
                "after reading guides.precise_prefix_cache_aware. Verify the "
                "recommendation's `cited_sources` and `summary`."
            )
        if not (inp.correctness.vllm_block_size and inp.correctness.vllm_hash_seed):
            warnings.append(
                "enable_precise_prefix_cache=true requires correctness.vllm_block_size "
                "AND correctness.vllm_hash_seed to be supplied (must match the "
                "deployed vLLM exactly). See guides.precise_prefix_cache_aware."
            )
    if not (inp.workload.isl or inp.workload.osl):
        warnings.append(
            "No ISL/OSL provided — recommendation less workload-specific."
        )

    # Benchmark config
    workload_class = classify_workload(inp.workload, inp.slo)
    benchmark_result = build_benchmark(inp, workload_class, harness=inp.bench_harness)
    warnings.extend(benchmark_result["warnings"])

    # Hash
    canonical = json.dumps(_input_to_dict(inp), sort_keys=True)
    input_hash = hashlib.sha256(canonical.encode()).hexdigest()[:16]

    # Chart-level toggles render_helm_values projects into the values.yaml
    # fragment. Keys are FULL dotted paths from the router chart's top-level
    # values namespace — e.g. "router.latencyPredictor.enabled" (nested under
    # the router subchart) vs "httpRoute.create" / "provider.name" (top-level
    # chart keys). render_helm_values splits on "." to nest.
    chart_toggles: dict = {}
    if inp.features.enable_latency_predictor:
        chart_toggles["router.latencyPredictor.enabled"] = True

    # Chart variant follows Context.deploy_mode (added in Phase C).
    chart_variant = (
        "gateway" if inp.context.deploy_mode == "gateway" else "standalone"
    )
    if inp.context.deploy_mode == "gateway":
        # httpRoute + provider are TOP-LEVEL chart values, not nested under
        # router. The gateway chart creates the HTTPRoute when create=true.
        chart_toggles["httpRoute.create"] = True
        chart_toggles["httpRoute.inferenceGatewayName"] = "llm-d-inference-gateway"
        # The chart's provider.name accepts {istio, kgateway, agentgateway, gke}.
        # Our GKE provider variants (gke-l7-rilb, gke-l7-regional-external-managed)
        # both map to provider.name=gke at the chart level.
        chart_toggles["provider.name"] = _chart_provider_name(inp.context.gateway_provider)

    return Output(
        input_hash=input_hash,
        decisions={
            "workload_class": workload_class,
            "epp": {
                "chart": chart_variant,
                "image_tag": "main",
                "endpoint_picker_config": epp_config,
                "chart_toggles": chart_toggles,
            },
            "benchmark": {
                "harness": benchmark_result["harness"],
                "harness_image": _BENCHMARK_HARNESS_IMAGE,
                "config": benchmark_result["config"],
            },
            "context": dataclasses.asdict(inp.context),
            # Phase C: features mirrored into decisions so render_bundle()
            # can decide which feature resources (WVA CR, HPA, etc.) to
            # emit without taking Input as a second arg.
            "features": dataclasses.asdict(inp.features),
            "topology": dataclasses.asdict(inp.topology),
            "model": inp.model,
        },
        rationale=rationale,
        parameters=[dataclasses.asdict(p) for p in inspect],
        unresolved_questions=unresolved,
        warnings=warnings,
    )


class _LiteralBlock(str):
    """str subclass that PyYAML emits as a `|` block scalar (used for the EPP
    config string, which the router chart reads as a ConfigMap entry)."""


def _literal_block_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


class _ValuesDumper(yaml.SafeDumper):
    """SafeDumper + the _LiteralBlock representer, scoped so the block-scalar
    style doesn't leak into other yaml.safe_dump calls."""


_ValuesDumper.add_representer(_LiteralBlock, _literal_block_representer)


def render_helm_values(out: Output) -> str:
    """Render an llm-d-router chart values.yaml fragment with the EPP config
    inlined under router.epp.pluginsCustomConfig.

    `decisions["epp"]["chart_toggles"]` keys carry their FULL dotted path from
    the chart's top-level values namespace — e.g. `router.latencyPredictor.enabled`
    (nested under the router subchart) vs `httpRoute.create` / `provider.name`
    (top-level chart keys). Each dotted key is split on "." and merged into the
    values tree. Our EPP config is layered under router.epp, where it is the
    canonical source-of-truth for this script.
    """
    epp_yaml = yaml.safe_dump(
        out.decisions["epp"]["endpoint_picker_config"],
        sort_keys=False,
    )

    # Build the values tree from chart_toggles by splitting each dotted key.
    values: dict = {}
    for dotted_key, val in out.decisions["epp"].get("chart_toggles", {}).items():
        cursor = values
        parts = dotted_key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = val

    # Our EPP config goes under router.epp. pluginsCustomConfig is a block
    # scalar so the chart reads it as a string (it becomes a ConfigMap entry).
    epp = values.setdefault("router", {}).setdefault("epp", {})
    epp["pluginsConfigFile"] = "epp-config.yaml"
    epp["pluginsCustomConfig"] = {"epp-config.yaml": _LiteralBlock(epp_yaml)}

    return yaml.dump(
        values, Dumper=_ValuesDumper, sort_keys=False, default_flow_style=False,
    )


# ---------------------------------------------------------------------------
# Phase C — Bundle renderer (helm template + hand-rendered resources)
# ---------------------------------------------------------------------------

# Default chart OCI URLs (the llm-d-router charts); override via CLI flag for
# forks / pinned versions. The chart variant follows deploy mode: `standalone`
# exposes the EPP as a Service, `gateway` sits behind a Kubernetes Gateway.
_ROUTER_CHART_OCI = {
    "standalone": "oci://ghcr.io/llm-d/charts/llm-d-router-standalone-dev",
    "gateway":    "oci://ghcr.io/llm-d/charts/llm-d-router-gateway-dev",
}
# Router chart version. Upstream publishes the router charts only as a rolling
# `-dev` build tagged `v0` (no immutable release yet), which is also what the
# optimized-baseline guide pins (`ROUTER_CHART_VERSION=v0`).
_ROUTER_CHART_DEFAULT_VERSION = "v0"

# llm-d repo ref for guide values + kustomize URLs the script fetches at run
# time. Tracks `main` — the router charts + their router-schema guide values
# live on main (upstream has not cut a release for this path yet). Override at
# runtime with --llm-d-ref to pin a branch/tag/SHA.
_LLM_D_REF = "main"

# llm-d-router repo ref for the CRD component kustomizations fetched by
# render_bundle's _fetch_crd_manifests. Tracks main alongside _LLM_D_REF.
# Override with --llm-d-router-ref.
_LLM_D_ROUTER_REF = "main"

def _chart_provider_name(gateway_provider: str | None) -> str:
    """Map our gateway_provider value to the router chart's provider.name.
    The chart accepts a coarser set than our provider taxonomy.
    """
    if gateway_provider is None:
        return "none"
    if gateway_provider.startswith("gke-"):
        return "gke"
    return gateway_provider


# Upstream values files that supply chart-required fields (e.g.
# router.modelServers.matchLabels) we don't compute ourselves. Layered BEFORE
# our autoconfig values so our pluginsCustomConfig overrides their defaults.
# `{ref}` is substituted at call time from the resolved llm_d_ref (default
# _LLM_D_REF, overridable via --llm-d-ref).
_RECIPE_BASE_VALUES_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/llm-d/llm-d/{ref}/"
    "guides/recipes/router/base.values.yaml"
)
_GUIDE_VALUES_URL_TEMPLATE = {
    "agg":    "https://raw.githubusercontent.com/llm-d/llm-d/{ref}/"
              "guides/optimized-baseline/router/optimized-baseline.values.yaml",
    "disagg": "https://raw.githubusercontent.com/llm-d/llm-d/{ref}/"
              "guides/pd-disaggregation/router/pd-disaggregation.values.yaml",
}


# autoconfig's own default name for the HF token Secret it scaffolds.
# Not an upstream convention — the canonical modelserver overlays at
# llm-d/guides/optimized-baseline/modelserver/gpu/vllm/base/patch-vllm.yaml
# leave HF token wiring up to the user (env block is commented out). We pick
# `llm-d-hf-token` because it's self-documenting (prefixed `llm-d-`), unlikely
# to collide with arbitrary ambient secret names, and matches what the older
# helm chart's `modelArtifacts.authSecretName` defaults to. Users can override
# at Phase 2 Q0.5 by pointing at an existing secret of any name.
_DEFAULT_HF_SECRET_NAME = "llm-d-hf-token"


def render_bundle(
    out: Output,
    *,
    helm_binary: str = "helm",
    kubectl_binary: str = "kubectl",
    chart_oci: str | None = None,
    chart_version: str = _ROUTER_CHART_DEFAULT_VERSION,
    llm_d_ref: str = _LLM_D_REF,
    llm_d_router_ref: str = _LLM_D_ROUTER_REF,
    include_crds: bool = True,
    subprocess_runner=None,
) -> str:
    """Render a multi-document YAML bundle ready for `kubectl apply -f`.

    Composes (in apply order):
      0. CRDs via `kubectl kustomize` against llm-d-router/deploy/components/
         (Gateway API + GIE + provider-specific). Skip with include_crds=False.
      1. `helm template` output of the llm-d-router chart variant (standalone
         or gateway) with the values fragment applied.
      2. Hand-rendered HTTPRoute when deploy_mode='standalone' AND a
         gateway_provider is set (standalone chart doesn't emit one).
      3. Phase B feature resources (WVA CR, HPA, InferenceObjective, etc.).

    Args:
      out: The build_output() result.
      helm_binary: Path/name of helm CLI. Defaults to "helm" on PATH.
      kubectl_binary: Path/name of kubectl CLI (used for `kubectl kustomize`
        to fetch CRDs). Defaults to "kubectl" on PATH.
      chart_oci: Override OCI URL. Default picks from epp.chart variant.
      chart_version: Pinned chart version.
      include_crds: If True (default), prepend Gateway API + GIE CRDs to
        the bundle so a fresh cluster can `kubectl apply -f` end-to-end.
        Set False when the cluster already has the CRDs installed (e.g.,
        operator-managed) and you want a shorter bundle.
      subprocess_runner: Injected subprocess.run for testing. Default uses
        the real subprocess module.

    Raises:
      FileNotFoundError: helm or kubectl binary not found.
      subprocess.CalledProcessError: helm template OR kubectl kustomize
        failed — caller surfaces stderr to user.
    """
    import subprocess

    runner = subprocess_runner or subprocess.run

    ctx = out.decisions["context"]
    namespace = ctx["namespace"]
    release_name = ctx["release_name"]
    deploy_mode = ctx.get("deploy_mode", "standalone")
    gateway_provider = ctx.get("gateway_provider")

    chart_variant = out.decisions["epp"]["chart"]
    if chart_oci is None:
        chart_oci = _ROUTER_CHART_OCI[chart_variant]

    helm_values = render_helm_values(out)

    # Layered values: recipe base + guide values (from topology.mode) + our
    # autoconfig values (LAST so its pluginsCustomConfig overrides). The
    # recipe + guide files come straight from raw.githubusercontent.com;
    # helm fetches them inline via -f <URL>. Our values come via -f - on
    # stdin so the call stays temp-file-free.
    topology_mode = out.decisions.get("topology", {}).get("mode", "agg")
    cmd = [
        helm_binary, "template", release_name, chart_oci,
        "--version", chart_version,
        "--namespace", namespace,
        "-f", _RECIPE_BASE_VALUES_URL_TEMPLATE.format(ref=llm_d_ref),
        "-f", _GUIDE_VALUES_URL_TEMPLATE[topology_mode].format(ref=llm_d_ref),
        "-f", "-",
    ]
    result = runner(
        cmd, input=helm_values, capture_output=True, text=True, check=True,
    )
    helm_yaml = result.stdout

    # CRDs first — every Gateway / InferencePool / etc. resource depends on
    # the corresponding type being registered. Fetched via kubectl kustomize
    # against llm-d-router/deploy/components/. Filename-rank 0 puts them
    # before everything else in `kubectl apply -f <dir>` ordering.
    crd_yaml: str = ""
    if include_crds:
        crd_yaml = _fetch_crd_manifests(
            gateway_provider=gateway_provider,
            kubectl_binary=kubectl_binary,
            llm_d_router_ref=llm_d_router_ref,
            subprocess_runner=subprocess_runner,
        )

    # Phase C.8: prereqs come FIRST so kubectl apply can create the
    # namespace + Secret scaffolds in the same pass as the chart resources.
    # Namespace is idempotent (apply of existing is a no-op).
    #
    # HF Secret scaffold conditions (see Context.hf_secret_name docstring):
    #   1. The user picked "scaffold new" at Phase 2 Q0.5
    #      (signalled by hf_secret_name == _DEFAULT_HF_SECRET_NAME), AND
    #   2. The default-named Secret doesn't already exist in target ns
    #      (signalled by hf_secret_exists = False).
    # Skipping condition #2 would let `kubectl apply` overwrite a working
    # token with empty stringData on re-apply — Secrets go through the
    # same 3-way merge as any resource; the `stringData` field is
    # replacement-on-key. See Context docstring for the full rationale.
    prereq_docs: list[str] = []
    if namespace != "default":
        prereq_docs.append(_render_namespace(namespace))
    hf_secret_name = ctx.get("hf_secret_name")
    hf_secret_exists = ctx.get("hf_secret_exists", False)
    should_render_scaffold = (
        hf_secret_name == _DEFAULT_HF_SECRET_NAME
        and not hf_secret_exists
    )
    if should_render_scaffold:
        prereq_docs.append(_render_hf_token_secret(namespace, name=hf_secret_name))

    extra_docs: list[str] = []

    # Gateway resource — needed whenever the user picks a gateway provider
    # (either deploy_mode=gateway or deploy_mode=standalone with a provider
    # set). The chart never renders this; it has to come from us so the
    # bundle is self-contained for a fresh cluster. Name matches the
    # `httpRoute.inferenceGatewayName` chart toggle ("llm-d-
    # inference-gateway"), so the chart-emitted HTTPRoute (gateway mode) and
    # our hand-rendered HTTPRoute (standalone+provider mode) both bind to it.
    if gateway_provider is not None:
        extra_docs.append(_render_gateway(
            namespace=namespace,
            gateway_provider=gateway_provider,
        ))

    # Hand-rendered HTTPRoute for standalone + gateway-provider case.
    # (gateway-mode HTTPRoute comes from the chart via httpRoute.create.)
    if deploy_mode == "standalone" and gateway_provider is not None:
        extra_docs.append(_render_httproute(
            release_name=release_name,
            namespace=namespace,
            gateway_provider=gateway_provider,
        ))

    # Modelserver pods are intentionally OUT of the bundle. The skill's scope
    # is the EPP + gateway layer; modelserver deploys are a separate concern
    # handled by Phase 6.3 of the SKILL (`kubectl apply -k <upstream-overlay>`).
    # Reasons:
    #   1. The upstream overlay has hardware/model-specific variants we don't
    #      template (nvidia-gpu vs TPU, vllm vs SGLang, per-model values).
    #   2. Inlining `kubectl kustomize` output would produce YAMLs that often
    #      need post-render editing — false "self-contained" promise.
    #   3. The `context.modelserver_deploy_planned` field is consumed by Phase 3
    #      (skips the schedulability audit when False) but does not affect
    #      bundle rendering.

    # Feature-resource rendering (autoscalers, kustomization scaffolds,
    # InferenceObjective/ModelRewrite CRs).
    feature_todos = _render_feature_todos(out)
    if feature_todos:
        extra_docs.append(feature_todos)

    parts: list[str] = []
    if crd_yaml:
        parts.append(crd_yaml.rstrip())
    for doc in prereq_docs:
        parts.append(doc.rstrip())
    parts.append(helm_yaml.rstrip())
    for doc in extra_docs:
        parts.append(doc.rstrip())
    # Separator: newline + --- + newline. rstrip on each part means there's
    # no trailing newline before "---" otherwise, which glues the marker to
    # the previous doc's last value.
    return "\n---\n".join(parts) + "\n"


def _render_namespace(namespace: str) -> str:
    """Render a Namespace resource. Idempotent under `kubectl apply` —
    safe to include even when the namespace already exists. Skipped for
    the `default` namespace (Kubernetes creates that itself)."""
    ns = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "annotations": {"llm-d.ai/generated-by": "autoconfig"},
        },
    }
    return yaml.safe_dump(ns, sort_keys=False)


def _render_hf_token_secret(namespace: str, *, name: str = _DEFAULT_HF_SECRET_NAME) -> str:
    """Render an HF token Secret SCAFFOLD with EMPTY stringData. User must
    fill in HF_TOKEN before model server pulls gated weights. Public-only
    models can apply as-is.

    The `name` defaults to autoconfig's own scaffold name
    (`_DEFAULT_HF_SECRET_NAME` = "llm-d-hf-token"). This is NOT an upstream
    convention — see the `_DEFAULT_HF_SECRET_NAME` constant for the audit
    trail. Callers should only invoke this when render_bundle's scaffold
    conditions hold (user picked "scaffold new" AND no Secret with the
    default name already exists).
    """
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": {
                "llm-d.ai/generated-by": "autoconfig",
                "llm-d.ai/scaffold": (
                    f"fill HF_TOKEN before applying for gated models. Public "
                    f"models work with an empty value. Edit via `kubectl edit "
                    f"secret {name} -n <ns>` after apply, OR delete "
                    f"this doc from the bundle and create the Secret separately."
                ),
            },
        },
        "type": "Opaque",
        "stringData": {"HF_TOKEN": ""},
    }
    return yaml.safe_dump(secret, sort_keys=False)


# gateway_provider → gatewayClassName mapping. Our gateway_provider taxonomy
# is finer-grained than the chart's `provider.name` (the GKE provider variants
# all map to chart provider.name=gke), but Gateway resources use the actual
# gatewayClassName which IS finer-grained. Each value here is what shows up
# as a registered GatewayClass on the cluster.
_GATEWAY_CLASS_NAME: dict[str, str] = {
    "istio":                            "istio",
    "kgateway":                         "kgateway",
    "agentgateway":                     "agentgateway",
    "gke-l7-rilb":                      "gke-l7-rilb",
    "gke-l7-regional-external-managed": "gke-l7-regional-external-managed",
}


def _render_gateway(*, namespace: str, gateway_provider: str) -> str:
    """Hand-rendered Gateway resource. Name is `llm-d-inference-gateway` to
    match the chart's `httpRoute.inferenceGatewayName` toggle —
    keeps HTTPRoute (chart-emitted in gateway mode, hand-rendered in
    standalone+provider mode) bound to the same Gateway by name.

    HTTP listener on port 80 with same-namespace route admission. Users
    needing HTTPS / TLS or cross-namespace routing should `kubectl edit`
    after apply (the bundle is a starting point, not a final config).
    """
    gateway_class = _GATEWAY_CLASS_NAME.get(gateway_provider, gateway_provider)
    gw = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "Gateway",
        "metadata": {
            "name": "llm-d-inference-gateway",
            "namespace": namespace,
            "annotations": {
                "llm-d.ai/generated-by": "autoconfig",
                "llm-d.ai/gateway-provider": gateway_provider,
            },
        },
        "spec": {
            "gatewayClassName": gateway_class,
            "listeners": [{
                "name": "http",
                "port": 80,
                "protocol": "HTTP",
                "allowedRoutes": {"namespaces": {"from": "Same"}},
            }],
        },
    }
    return yaml.safe_dump(gw, sort_keys=False)


def _render_httproute(*, release_name: str, namespace: str, gateway_provider: str) -> str:
    """Hand-rendered HTTPRoute. Used in standalone + gateway-mode where the
    standalone chart doesn't emit one. The gatewayClassName / parentRefs
    name follows the convention from llm-d/guides/optimized-baseline.
    """
    route = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {
            "name": f"{release_name}-route",
            "namespace": namespace,
            "annotations": {
                "llm-d.ai/generated-by": "autoconfig",
                "llm-d.ai/gateway-provider": gateway_provider,
            },
        },
        "spec": {
            "parentRefs": [{"name": "llm-d-inference-gateway"}],
            "rules": [{
                "backendRefs": [{
                    "group": "inference.networking.x-k8s.io",
                    "kind": "InferencePool",
                    "name": release_name,
                    "port": 8080,
                }],
                "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                "timeouts": {"request": "0s"},
            }],
        },
    }
    return yaml.safe_dump(route, sort_keys=False)


def _render_feature_todos(out: Output) -> str:
    """For each Phase B feature flag the user enabled, emit either:
      - A concrete Kubernetes resource (e.g. VariantAutoscaling CR for WVA,
        HPA for the autoscaler=hpa case), OR
      - A scaffold/TODO comment block when the feature's deploy-side artifact
        needs a hand-edit (e.g. tiered-cache modelserver overlay).

    Returns a single string of YAML docs separated by `---`. Empty when no
    Phase B flags are on.
    """
    features = out.decisions.get("features", {})
    ctx = out.decisions["context"]
    release = ctx["release_name"]
    namespace = ctx["namespace"]
    model = out.decisions.get("model", "")
    topology = out.decisions.get("topology", {})

    docs: list[str] = []

    if features.get("autoscaler") == "wva":
        docs.append(_render_wva_variant_autoscaling(
            release=release, namespace=namespace, model=model, topology=topology,
        ))
    if features.get("autoscaler") == "hpa":
        docs.append(_render_hpa_for_inferencepool(
            release=release, namespace=namespace,
        ))
    if features.get("enable_tiered_cache"):
        docs.append(_render_tiered_cache_scaffold(release, namespace))
    if features.get("enable_wide_ep"):
        docs.append(_render_wide_ep_scaffold(release, namespace, model))
    if features.get("serving_pattern") == "batch":
        docs.append(_render_batch_gateway_scaffold(release, namespace))
    if features.get("serving_pattern") == "async":
        docs.append(_render_async_scaffold(release, namespace))
    if features.get("enable_inference_objective"):
        docs.append(_render_inference_objective(release, namespace))
    if features.get("enable_model_rewrite"):
        docs.append(_render_inference_model_rewrite(release, namespace, model))

    if not docs:
        return ""
    return "\n---\n".join(docs)


def _render_wva_variant_autoscaling(*, release: str, namespace: str,
                                    model: str, topology: dict) -> str:
    """Render a VariantAutoscaling CR per the WVA operator's CRD. Topology
    fields (replicas, tp) seed the variant. The user must still install the
    WVA operator; the CR alone doesn't bootstrap WVA."""
    mode = topology.get("mode", "agg")
    if mode == "disagg":
        # PD variants get a per-role entry.
        variants = [
            {
                "name": "prefill",
                "replicas": topology.get("prefill_replicas", 1),
                "modelServerArgs": {
                    "tensorParallelSize": topology.get("prefill_tp", 1),
                },
            },
            {
                "name": "decode",
                "replicas": topology.get("decode_replicas", 1),
                "modelServerArgs": {
                    "tensorParallelSize": topology.get("decode_tp", 1),
                },
            },
        ]
    else:
        variants = [{
            "name": "default",
            "replicas": topology.get("replicas", 1),
            "modelServerArgs": {
                "tensorParallelSize": topology.get("tp", 1),
            },
        }]

    cr = {
        "apiVersion": "autoscaling.llm-d.ai/v1alpha1",
        "kind": "VariantAutoscaling",
        "metadata": {
            "name": release,
            "namespace": namespace,
            "annotations": {"llm-d.ai/generated-by": "autoconfig"},
        },
        "spec": {
            "modelID": model,
            "inferencePoolRef": {"name": release},
            "variants": variants,
        },
    }
    return (
        "# WVA requires the workload-variant-autoscaler operator to be running.\n"
        "# Install separately: see guides.workload_autoscaling_wva.\n"
        f"{yaml.safe_dump(cr, sort_keys=False)}"
    )


def _render_hpa_for_inferencepool(*, release: str, namespace: str) -> str:
    """HPA-EPP pattern: HorizontalPodAutoscaler driven by EPP-emitted metrics.
    Uses the InferencePool's modelserver Deployment as the scaleTargetRef.
    Default thresholds; user tunes targetValue per workload."""
    hpa = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {
            "name": f"{release}-hpa",
            "namespace": namespace,
            "annotations": {"llm-d.ai/generated-by": "autoconfig"},
        },
        "spec": {
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": release,
            },
            "minReplicas": 1,
            "maxReplicas": 10,
            "metrics": [{
                "type": "Pods",
                "pods": {
                    "metric": {"name": "epp_queue_depth_avg"},
                    "target": {"type": "AverageValue", "averageValue": "5"},
                },
            }],
        },
    }
    return (
        "# HPA-EPP requires the EPP to expose epp_queue_depth_avg as a Pods\n"
        "# metric (Prometheus Adapter / custom-metrics-apiserver). See\n"
        "# guides.workload_autoscaling_hpa for the scrape config.\n"
        f"{yaml.safe_dump(hpa, sort_keys=False)}"
    )


def _render_tiered_cache_scaffold(release: str, namespace: str) -> str:
    """Tiered cache is a modelserver-side kustomize overlay. The guide forks
    by tier (cpu vs storage), accelerator (gpu, tpu), and connector (base,
    lmcache-connector, offloading-connector), so there's no single overlay to
    apply — the user picks. Comment-only advisory; nothing to kubectl-apply."""
    return (
        "# enable_tiered_cache: KV-cache offloading is a modelserver-side overlay.\n"
        "# Not auto-rendered — pick a tier (CPU RAM vs disk/shared storage), then\n"
        "# the accelerator + connector overlay, e.g.\n"
        "#   guides/tiered-prefix-cache/cpu/modelserver/gpu/vllm/offloading-connector\n"
        "# Apply it AFTER the modelservice base. See guides.tiered_prefix_cache.\n"
    )


def _render_wide_ep_scaffold(release: str, namespace: str, model: str) -> str:
    """Wide-EP requires LeaderWorkerSet — modelserver-side, MoE-only. The guide
    forks by accelerator and infra overlay (base, gke, coreweave, ...), so
    there's no single overlay to apply. Comment-only advisory."""
    return (
        f"# enable_wide_ep: requires a sparse-MoE model (yours is {model!r}; verify).\n"
        "# Not auto-rendered — LeaderWorkerSet wide-EP is a modelserver-side overlay,\n"
        "# forked by accelerator + infra, e.g.\n"
        "#   guides/wide-ep-lws/modelserver/gpu/vllm/base\n"
        "# Apply it after the modelservice base. See guides.wide_ep_lws.\n"
    )


def _render_batch_gateway_scaffold(release: str, namespace: str) -> str:
    return (
        "# serving_pattern=batch: batch-gateway is a separate gateway pattern.\n"
        "# Not auto-rendered — see guides.batch_gateway for the helmfile.\n"
        "# (Comment-only scaffold; nothing to kubectl-apply here.)\n"
    )


def _render_async_scaffold(release: str, namespace: str) -> str:
    return (
        "# serving_pattern=async: background/long-running request processing.\n"
        "# Not auto-rendered — see guides.asynchronous_processing.\n"
        "# (Comment-only scaffold; nothing to kubectl-apply here.)\n"
    )


def _render_inference_objective(release: str, namespace: str) -> str:
    """Render a minimal InferenceObjective CR — poolRef points at the
    release-named InferencePool. Default priority=0; user can `kubectl edit`
    to set a higher priority for the model to win admission. Group +
    version per llm-d-router/apix/v1alpha2/inferenceobjective_types.go."""
    cr = {
        "apiVersion": "inference.networking.x-k8s.io/v1alpha2",
        "kind": "InferenceObjective",
        "metadata": {
            "name": f"{release}-objective",
            "namespace": namespace,
            "annotations": {"llm-d.ai/generated-by": "autoconfig"},
        },
        "spec": {
            "priority": 0,
            "poolRef": {"name": release},
        },
    }
    return (
        "# InferenceObjective: per-model routing priority for the EPP.\n"
        "# priority=0 is the default; raise it for models that should win\n"
        "# admission under load. See schemas.inferenceobjective_crd.\n"
        f"{yaml.safe_dump(cr, sort_keys=False)}"
    )


def _render_inference_model_rewrite(release: str, namespace: str, model: str) -> str:
    """Render a minimal InferenceModelRewrite CR — rewrites all incoming
    requests to the configured model. The `modelRewrite` target is set to
    the canonical model the EPP is configured for; the rule has no
    `matches`, so it applies to every request. Group + version per
    llm-d-router/apix/v1alpha2/inferencemodelrewrite_types.go."""
    cr = {
        "apiVersion": "inference.networking.x-k8s.io/v1alpha2",
        "kind": "InferenceModelRewrite",
        "metadata": {
            "name": f"{release}-rewrite",
            "namespace": namespace,
            "annotations": {"llm-d.ai/generated-by": "autoconfig"},
        },
        "spec": {
            "poolRef": {"name": release},
            "rules": [{
                "targets": [{"modelRewrite": model}],
            }],
        },
    }
    return (
        "# InferenceModelRewrite: rewrites incoming model names to the canonical\n"
        f"# pool entry ({model!r}). The default rule has no matches, so it applies\n"
        "# to every request — narrow with spec.rules[].matches[].model.value if\n"
        "# you want selective rewrite. See schemas.inferencemodelrewrite_crd.\n"
        f"{yaml.safe_dump(cr, sort_keys=False)}"
    )


# ---------------------------------------------------------------------------
# Bundle output as a directory of individual YAMLs
# ---------------------------------------------------------------------------

# Apply ordering for `kubectl apply -f <dir>`. kubectl applies files in
# alphabetical order, so the numeric prefix dictates the deploy sequence.
# CRDs are first (rank 0) — every resource that uses a CRD-defined type must
# wait for the type to register. Then admission policies, then prereqs
# (Namespace, Secret), then RBAC + chart resources, then routes, then
# feature/CR overlays. Unknown kinds default to 99.
_RESOURCE_ORDER: dict[str, int] = {
    "CustomResourceDefinition": 0,
    "ValidatingAdmissionPolicy": 2,
    "ValidatingAdmissionPolicyBinding": 3,
    "Namespace": 5,
    "Secret": 7,
    "ConfigMap": 10,
    "ServiceAccount": 12,
    "Role": 14,
    "ClusterRole": 14,
    "RoleBinding": 16,
    "ClusterRoleBinding": 16,
    "Service": 20,
    "Deployment": 30,
    "InferencePool": 35,
    # Gateway must exist BEFORE the HTTPRoute that binds to it via parentRefs.
    "Gateway": 45,
    "HTTPRoute": 50,
    "DestinationRule": 52,
    "InferenceObjective": 60,
    "InferenceModelRewrite": 62,
    "VariantAutoscaling": 70,
    "HorizontalPodAutoscaler": 72,
    "Kustomization": 80,
}

# CRD kustomize sources on llm-d-router. `crds-gateway-api` + `crds-gie`
# are always required (Gateway API + InferencePool/InferenceObjective).
# `crds-istio` is gateway-provider-specific. `{ref}` is filled from the
# resolved llm_d_router_ref (default _LLM_D_ROUTER_REF, override via
# --llm-d-router-ref).
_CRD_KUSTOMIZE_SOURCES_TEMPLATE: dict[str, str] = {
    "gateway-api": "https://github.com/llm-d/llm-d-router.git/deploy/components/crds-gateway-api?ref={ref}",
    "gie":         "https://github.com/llm-d/llm-d-router.git/deploy/components/crds-gie?ref={ref}",
}
_CRD_KUSTOMIZE_PROVIDER_SOURCES_TEMPLATE: dict[str, str] = {
    "istio": "https://github.com/llm-d/llm-d-router.git/deploy/components/crds-istio?ref={ref}",
}


def _fetch_crd_manifests(
    *,
    gateway_provider: str | None,
    kubectl_binary: str = "kubectl",
    llm_d_router_ref: str = _LLM_D_ROUTER_REF,
    subprocess_runner=None,
) -> str:
    """Fetch CRD manifests via `kubectl kustomize` against the llm-d-router
    component dirs. Returns concatenated multi-doc YAML (one CRD per doc).
    Empty string if no kustomize sources apply.

    Raises FileNotFoundError if kubectl is missing, or CalledProcessError
    if kustomize itself fails (network error, bad ref, etc.). Callers
    decide whether to fail or skip on those.
    """
    import subprocess
    runner = subprocess_runner or subprocess.run

    sources = [t.format(ref=llm_d_router_ref) for t in _CRD_KUSTOMIZE_SOURCES_TEMPLATE.values()]
    if gateway_provider in _CRD_KUSTOMIZE_PROVIDER_SOURCES_TEMPLATE:
        sources.append(
            _CRD_KUSTOMIZE_PROVIDER_SOURCES_TEMPLATE[gateway_provider].format(ref=llm_d_router_ref)
        )

    chunks: list[str] = []
    for src in sources:
        result = runner(
            [kubectl_binary, "kustomize", src],
            capture_output=True, text=True, check=True,
        )
        chunks.append(result.stdout.rstrip())
    return "\n---\n".join(chunks)


def _sanitize_filename_part(s: str) -> str:
    """Make a string filesystem-safe: lowercase, ASCII, dashes only."""
    return "".join(c if (c.isalnum() or c in "-.") else "-" for c in s.lower()).strip("-")


def _resource_filename(doc: dict, index: int) -> str:
    """Build a deterministic filename for a single resource doc.

    Format: `NN-MM-<kind>-<name>.yaml` where NN is the kind-order rank (for
    apply sequencing), MM is the doc's position in the bundle (disambiguates
    same-kind resources), and the rest identifies the resource.
    """
    kind = doc.get("kind", "unknown")
    name = doc.get("metadata", {}).get("name", "unnamed")
    rank = _RESOURCE_ORDER.get(kind, 99)
    return f"{rank:02d}-{index:02d}-{_sanitize_filename_part(kind)}-{_sanitize_filename_part(name)}.yaml"


def render_bundle_dir(
    out: Output,
    *,
    parent_dir: Path,
    timestamp: str | None = None,
    helm_binary: str = "helm",
    kubectl_binary: str = "kubectl",
    chart_oci: str | None = None,
    chart_version: str = _ROUTER_CHART_DEFAULT_VERSION,
    llm_d_ref: str = _LLM_D_REF,
    llm_d_router_ref: str = _LLM_D_ROUTER_REF,
    include_crds: bool = True,
    subprocess_runner=None,
) -> Path:
    """Render the bundle into a timestamped sub-directory of individual YAMLs.

    Creates `<parent_dir>/autoconfig-<TIMESTAMP>/` containing:
      - One `NN-MM-<kind>-<name>.yaml` per resource (numeric prefix orders
        the apply sequence; same prefix collisions disambiguated by MM).
      - `README.md` with input_hash, generation timestamp, apply hint, and
        any comment-only scaffold notes from the bundle (e.g. batch/async
        guidance that doesn't ship as a K8s resource).

    Args:
      parent_dir: Where to create the timestamped subdir. Must exist.
      timestamp: Override the generated timestamp (useful for tests).
        Default: current UTC time in `%Y%m%dT%H%M%S` format.
      helm_binary / chart_oci / chart_version / subprocess_runner: passed
        through to render_bundle().

    Returns the path to the created directory.

    Raises:
      FileExistsError: timestamped subdir already exists (rare collision
        within the same second; pass `timestamp` explicitly to retry).
    """
    import datetime

    if timestamp is None:
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")

    bundle_text = render_bundle(
        out,
        helm_binary=helm_binary,
        kubectl_binary=kubectl_binary,
        chart_oci=chart_oci,
        chart_version=chart_version,
        llm_d_ref=llm_d_ref,
        llm_d_router_ref=llm_d_router_ref,
        include_crds=include_crds,
        subprocess_runner=subprocess_runner,
    )

    out_dir = parent_dir / f"autoconfig-{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=False)

    # Split the multi-doc bundle into individual docs. Track both real K8s
    # resources and any comment-only scaffold blocks (the bundle includes
    # batch / async scaffolds that yaml.safe_load_all parses as None).
    scaffold_notes: list[str] = []
    docs_written = 0
    for index, raw_doc in enumerate(yaml.safe_load_all(bundle_text)):
        if raw_doc is None:
            # Comment-only scaffold — grab the leading comment block from the
            # raw text and stash it for the README. yaml.safe_load_all already
            # parsed it as nothing, so we have to re-scan. Simpler: walk the
            # bundle text by splitting on "---" and pair up.
            continue
        if not isinstance(raw_doc, dict) or "kind" not in raw_doc:
            continue
        filename = _resource_filename(raw_doc, index)
        (out_dir / filename).write_text(
            yaml.safe_dump(raw_doc, sort_keys=False),
            encoding="utf-8",
        )
        docs_written += 1

    # Re-scan the bundle text for OUR comment-only scaffold blocks (those
    # that parsed to None above). Our scaffolds mention guides.* or schemas.*
    # entries from feature_docs.yaml or use distinctive markers — that filter
    # excludes helm's own `# Source: <chart>/templates/...` headers, which
    # would otherwise be picked up as noise.
    _OUR_SCAFFOLD_MARKERS = (
        "guides.", "schemas.", "feature_docs.yaml",
        "Phase C.5", "comment scaffold", "Comment-only scaffold",
        "Not auto-rendered", "apply this kustomization",
    )
    for raw_block in bundle_text.split("\n---\n"):
        stripped = raw_block.strip()
        if not stripped:
            continue
        lines = [ln for ln in stripped.splitlines() if ln.strip()]
        # Pure comment block only — never re-include real YAML resources here.
        if not lines or not all(ln.lstrip().startswith("#") for ln in lines):
            continue
        if any(marker in stripped for marker in _OUR_SCAFFOLD_MARKERS):
            scaffold_notes.append(stripped)

    readme = _bundle_dir_readme(
        out=out,
        timestamp=timestamp,
        docs_written=docs_written,
        scaffold_notes=scaffold_notes,
        chart_oci=chart_oci or _ROUTER_CHART_OCI[out.decisions["epp"]["chart"]],
        chart_version=chart_version,
    )
    (out_dir / "README.md").write_text(readme, encoding="utf-8")

    return out_dir


def _bundle_dir_readme(
    *,
    out: Output,
    timestamp: str,
    docs_written: int,
    scaffold_notes: list[str],
    chart_oci: str,
    chart_version: str,
) -> str:
    """Render the README.md that ships alongside the per-resource YAMLs."""
    ctx = out.decisions["context"]
    lines = [
        "# Autoconfig deployment bundle",
        "",
        f"Generated: `{timestamp}` (UTC)",
        f"Input hash: `{out.input_hash}`",
        f"Script version: `{out.version}`",
        f"Chart: `{chart_oci}` @ `{chart_version}`",
        f"Namespace: `{ctx['namespace']}`  Release: `{ctx['release_name']}`",
        f"Deploy mode: `{ctx['deploy_mode']}`  Gateway provider: `{ctx['gateway_provider']}`",
        "",
        f"This directory contains {docs_written} Kubernetes resource(s) as individual YAML files,",
        "numerically prefixed for apply ordering. Apply with:",
        "",
        "```bash",
        "kubectl apply -f .",
        "```",
        "",
        "or apply a single resource:",
        "",
        "```bash",
        "kubectl apply -f 30-*-deployment-*.yaml",
        "```",
        "",
        "Filename format: `<order>-<index>-<kind>-<name>.yaml`. Lower order = applied earlier.",
        "",
        f"**HF token Secret:** `context.hf_secret_name` = `{ctx.get('hf_secret_name')}`, "
        f"already exists in target ns: `{ctx.get('hf_secret_exists', False)}`. "
        f"If the bundle includes a `05-*-secret-{ctx.get('hf_secret_name') or _DEFAULT_HF_SECRET_NAME}.yaml` "
        f"scaffold doc, edit it before applying to set your HF token for gated models. "
        f"Public models work with the empty default. If no scaffold is present, autoconfig "
        f"is referencing an existing Secret in the cluster (or none, if the model is public).",
        "",
        "## Benchmark",
        "",
        "- `autoconfig-benchmark.yaml` — workload config for the chosen harness. "
        "Not applied by `kubectl apply -f .` (it's not a K8s resource); feed it "
        "to the harness directly. See Phase 7 in the skill runbook.",
        "- `autoconfig-benchmark-job.yaml` — present when generated with "
        "`--bench-target` + `--bench-namespace`. This IS a K8s Job + ConfigMap; "
        "apply with `kubectl apply -f autoconfig-benchmark-job.yaml` after the EPP is Ready.",
        "",
    ]
    if scaffold_notes:
        lines.append("## Phase B feature notes (not auto-rendered)")
        lines.append("")
        lines.append(
            "Some enabled feature flags don't produce a K8s resource — they need a "
            "separate deploy step. The notes below come straight from the script's "
            "advisory output."
        )
        lines.append("")
        for note in scaffold_notes:
            lines.append("```")
            lines.append(note)
            lines.append("```")
            lines.append("")
    if out.warnings:
        lines.append("## Warnings from the script")
        lines.append("")
        for w in out.warnings:
            lines.append(f"- {w}")
        lines.append("")
    return "\n".join(lines) + "\n"


def output_to_dict(out: Output) -> dict:
    """Stable dict form of Output."""
    return {
        "version": out.version,
        "input_hash": out.input_hash,
        "decisions": out.decisions,
        "rationale": out.rationale,
        "parameters": out.parameters,
        "unresolved_questions": out.unresolved_questions,
        "warnings": out.warnings,
        "errors": out.errors,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render an EPP EndpointPickerConfig + benchmark config from agent-supplied input.",
    )
    parser.add_argument("--input", "-i", type=Path, help="Input JSON file (default: stdin)")
    parser.add_argument("--output", "-o", type=Path, help="Output JSON file (default: stdout)")
    parser.add_argument("--render-yaml", action="store_true",
                        help="Emit rendered EndpointPickerConfig YAML on stderr")
    parser.add_argument("--render-helm-values", action="store_true",
                        help="Emit an llm-d-router chart values.yaml fragment on stderr")
    parser.add_argument("--helm-values-out", type=Path,
                        help="Write the helm values fragment to this file")
    parser.add_argument("--render-benchmark", action="store_true",
                        help="Emit rendered benchmark YAML on stderr")
    parser.add_argument("--benchmark-out", type=Path,
                        help="Write rendered benchmark YAML to this file")
    parser.add_argument("--benchmark-deployment-out", type=Path,
                        help="Write a complete K8s Job + ConfigMap YAML to this file")
    parser.add_argument("--bench-target", type=str, help="Target URL for the benchmark Job")
    parser.add_argument("--bench-namespace", type=str, help="Namespace for the benchmark Job")
    parser.add_argument("--bench-pvc", type=str, default=None,
                        help="Name of an existing PVC to mount at /workspace")
    parser.add_argument("--bench-harness", choices=["guidellm", "inference-perf"], default=None,
                        help="Override input JSON's bench_harness field")
    parser.add_argument("--bundle-dir", type=Path,
                        help="Phase C: render the kubectl-apply-ready bundle as one YAML per "
                             "resource inside <parent>/autoconfig-<TIMESTAMP>/. Produces a "
                             "README.md with apply hints + scaffold notes. Apply with "
                             "`kubectl apply -f <parent>/autoconfig-<TIMESTAMP>/`. "
                             "Requires `helm` and `kubectl` on PATH at generation time; "
                             "only `kubectl` is needed at apply time.")
    parser.add_argument("--helm-binary", type=str, default="helm",
                        help="Path to helm CLI (default: 'helm' on PATH)")
    parser.add_argument("--kubectl-binary", type=str, default="kubectl",
                        help="Path to kubectl CLI for `kubectl kustomize` CRD fetch "
                             "(default: 'kubectl' on PATH)")
    parser.add_argument("--chart-version", type=str, default=_ROUTER_CHART_DEFAULT_VERSION,
                        help=f"llm-d-router chart version (default: {_ROUTER_CHART_DEFAULT_VERSION})")
    parser.add_argument("--llm-d-ref", type=str, default=_LLM_D_REF,
                        help=f"llm-d/llm-d git ref for guide values URLs the "
                             f"script fetches (default: {_LLM_D_REF}). Pinned "
                             f"to a release rather than `main` so upstream "
                             f"restructures don't silently break us.")
    parser.add_argument("--llm-d-router-ref", type=str, default=_LLM_D_ROUTER_REF,
                        help=f"llm-d/llm-d-router git ref for the CRD kustomize "
                             f"URLs used by --bundle-dir (default: {_LLM_D_ROUTER_REF}).")
    parser.add_argument("--no-crds", action="store_true",
                        help="Skip prepending Gateway API + GIE CRDs to the bundle. "
                             "Use when the cluster already has them installed; "
                             "default includes CRDs so a fresh cluster can apply the "
                             "bundle end-to-end.")
    args = parser.parse_args()

    if args.benchmark_deployment_out:
        if not args.bench_target or not args.bench_namespace:
            print(
                "error: --benchmark-deployment-out requires --bench-target and --bench-namespace",
                file=sys.stderr,
            )
            return 1

    raw = args.input.read_text() if args.input else sys.stdin.read()

    try:
        inp = parse_input(raw)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.bench_harness:
        inp.bench_harness = args.bench_harness
        try:
            inp.validate()
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    try:
        out = build_output(inp)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    out_json = json.dumps(output_to_dict(out), indent=2)
    if args.output:
        args.output.write_text(out_json + "\n")
    else:
        print(out_json)

    if args.render_yaml:
        epp_yaml = yaml.safe_dump(
            out.decisions["epp"]["endpoint_picker_config"],
            sort_keys=False,
        )
        sys.stderr.write("---\n# EndpointPickerConfig (YAML rendering)\n")
        sys.stderr.write(epp_yaml)

    if args.render_helm_values or args.helm_values_out:
        helm_values = render_helm_values(out)
        if args.helm_values_out:
            args.helm_values_out.write_text(helm_values)
        if args.render_helm_values:
            sys.stderr.write("---\n# Helm values fragment\n")
            sys.stderr.write(helm_values)

    benchmark_yaml: str | None = None
    if args.render_benchmark or args.benchmark_out:
        benchmark_yaml = yaml.safe_dump(
            out.decisions["benchmark"]["config"],
            sort_keys=False,
        )

    if args.render_benchmark:
        sys.stderr.write("---\n# Benchmark workload YAML\n")
        sys.stderr.write(benchmark_yaml)

    if args.benchmark_out:
        args.benchmark_out.write_text(benchmark_yaml)

    if args.benchmark_deployment_out:
        deployment_yaml = build_benchmark_deployment(
            out.decisions["benchmark"]["config"],
            target_url=args.bench_target,
            namespace=args.bench_namespace,
            pvc_name=args.bench_pvc,
        )
        args.benchmark_deployment_out.write_text(deployment_yaml)

    if args.bundle_dir:
        import subprocess
        include_crds = not args.no_crds
        args.bundle_dir.mkdir(parents=True, exist_ok=True)
        try:
            created = render_bundle_dir(
                out,
                parent_dir=args.bundle_dir,
                helm_binary=args.helm_binary,
                kubectl_binary=args.kubectl_binary,
                chart_version=args.chart_version,
                llm_d_ref=args.llm_d_ref,
                llm_d_router_ref=args.llm_d_router_ref,
                include_crds=include_crds,
            )
        except FileNotFoundError as e:
            missing = "helm" if "helm" in str(e) else "kubectl"
            print(
                f"error: --bundle-dir requires `{missing}` on PATH "
                f"(or --no-crds to skip the kustomize call): {e}",
                file=sys.stderr,
            )
            return 1
        except subprocess.CalledProcessError as e:
            print(f"error: bundle render failed: {e.stderr.strip()}", file=sys.stderr)
            return 1

        # Write the benchmark YAML into the bundle dir so users get a fully
        # self-contained workspace (avoids the manual stdout-capture step
        # that --render-benchmark alone forces). Always emit the workload
        # config; emit the K8s Job manifest only when --bench-target +
        # --bench-namespace make it deployable.
        bench_yaml = yaml.safe_dump(
            out.decisions["benchmark"]["config"], sort_keys=False,
        )
        (created / "autoconfig-benchmark.yaml").write_text(bench_yaml)
        if args.bench_target and args.bench_namespace:
            deployment_yaml = build_benchmark_deployment(
                out.decisions["benchmark"]["config"],
                target_url=args.bench_target,
                namespace=args.bench_namespace,
                pvc_name=args.bench_pvc,
            )
            (created / "autoconfig-benchmark-job.yaml").write_text(deployment_yaml)

        print(f"wrote bundle to {created}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
