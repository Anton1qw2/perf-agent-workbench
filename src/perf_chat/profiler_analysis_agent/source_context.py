import time

from ..runtime import execution_deadline, remaining_timeout
from .source_contracts import SourceContext, SourceSnapshot


def prepare_source(gateway, request):
    if request is None:
        return None
    started = time.monotonic()
    base = {
        "repository": request.repository,
        "requested_ref": request.ref,
        "requested_commit": request.commit,
        "selection_basis": "user_declared" if request.commit else "branch_tip",
    }
    if request.policy == "off":
        return SourceSnapshot(status="disabled", reason="Source enrichment disabled", **base)
    try:
        if request.commit and (
            request.scope_repository != request.repository or request.scope_ref != request.ref
        ):
            raise ValueError("Explicit commit selection has a different repository/branch scope")
        if gateway is None:
            raise ValueError("No registered Git source gateway")
        with execution_deadline(time.monotonic() + remaining_timeout(5)):
            commit, tree = gateway.resolve_revision(
                request.repository, request.commit or request.ref
            )
        return SourceSnapshot(
            status="resolved",
            commit=commit,
            tree=tree,
            preparation_seconds=time.monotonic() - started,
            **base,
        )
    except Exception:
        return SourceSnapshot(
            status="unavailable",
            reason="Source revision unavailable; no branch substitution was made",
            preparation_seconds=time.monotonic() - started,
            **base,
        )


def collect_source(gateway, request, snapshot, *, model_enabled):
    context = SourceContext(snapshot=snapshot)
    if request is None or request.policy == "off":
        if request is not None:
            context.limitations.append("Source enrichment explicitly disabled.")
        return context
    required = request.policy == "required"
    context.required_delivery = (
        "pending"
        if required and model_enabled
        else ("interpretation_disabled" if required else "not_required")
    )
    context.limitations.append("Source commit/profile-build correspondence is unverified.")
    if snapshot is None or snapshot.status != "resolved":
        context.limitations.append(snapshot.reason if snapshot else "Source was not prepared")
        context.needs_selection = required and model_enabled
        return context
    try:
        # Leave at least one second for model/report work; do not spend the parent budget on Git.
        left = remaining_timeout(6)
        if left <= 1:
            raise TimeoutError
        child_budget = min(5 - snapshot.preparation_seconds, left - 1)
        if child_budget <= 0:
            raise TimeoutError
        with execution_deadline(time.monotonic() + child_budget):
            locations = list(request.locations)
            if not locations:
                candidates = []
                for symbol in request.symbols:
                    candidates.extend(
                        gateway.find_source(request.repository, snapshot.commit, symbol)
                    )
                    if len(candidates) >= 20:
                        break
                context.candidates = candidates[:20]
                if not required:
                    locations = context.candidates[:5]
                if candidates:
                    context.limitations.append(
                        "Text matches are source candidates, not verified hotspot implementations."
                    )
            seen, total = set(), 0
            for location in locations[:5]:
                item = gateway.read_source(
                    request.repository,
                    snapshot.commit,
                    location,
                    candidate=not bool(request.locations),
                )
                length = len(item.text.encode("utf-8"))
                if total + length > 32768:
                    context.limitations.append("Source excerpt byte budget reached.")
                    break
                if item.id not in seen:
                    context.evidence.append(item)
                    seen.add(item.id)
                    total += length
    except Exception:
        context.limitations.append(
            "Source collection unavailable or its child budget expired; metric evidence is retained."
        )
    if not context.evidence:
        context.limitations.append(
            "No selected source range was available; specify a path and line range."
        )
    if (
        required
        and model_enabled
        and not any(s.resolution == "provided_location" for s in context.evidence)
    ):
        context.needs_selection = True
    if not model_enabled:
        context.limitations.append(
            "interpretation_disabled: source evidence was not delivered to a model."
        )
    return context
