"""Model access decisions.

A model is usable on a *surface* (``chat`` or ``api``) when every gate
passes: it is rolled out, the capability policies that apply to it (uncensored,
image generation) allow the user, each of its categories that applies to the
surface allows the user, and its own model policy allows the user.
Administrators bypass all gates.

``AccessContext`` loads the policies and the user's list memberships once, so
checking a whole catalog costs a constant number of queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bananachat.db import access as access_db
from bananachat.db import catalog


@dataclass
class Gate:
    scope: str
    resource_id: int
    label: str
    allowed: bool
    mode: str | None = None
    requests_enabled: bool = False
    pending_request: bool = False

    @property
    def requestable(self) -> bool:
        return (not self.allowed and self.requests_enabled and not self.pending_request
                and self.scope in access_db.SCOPES)


@dataclass
class AccessContext:
    user: object
    policies: dict = field(default_factory=dict)
    memberships: dict = field(default_factory=dict)
    pending: set = field(default_factory=set)
    categories: dict = field(default_factory=dict)

    @classmethod
    def load(cls, user) -> "AccessContext":
        context = cls(user=user, categories=catalog.categories_by_model())
        if user is not None and user["role"] != "admin":
            context.policies = access_db.all_policies()
            context.memberships = access_db.user_memberships(user["id"])
            context.pending = access_db.pending_requests_for(user["id"])
        return context

    @property
    def is_admin(self) -> bool:
        return self.user is not None and self.user["role"] == "admin"

    def policy(self, scope: str, resource_id: int) -> dict:
        return self.policies.get((scope, resource_id)) or access_db.default_policy(scope, resource_id)

    def gate(self, scope: str, resource_id: int, label: str) -> Gate:
        policy = self.policy(scope, resource_id)
        lists = self.memberships.get((scope, resource_id), set())
        return Gate(scope, resource_id, label, access_db.is_allowed(policy, lists), policy["mode"],
                    bool(policy["requests_enabled"]), (scope, resource_id) in self.pending)

    def allows(self, scope: str, resource_id: int = 0) -> bool:
        if self.user is None:
            return False
        return self.is_admin or self.gate(scope, resource_id, "").allowed

    def model_gates(self, model, surface: str) -> list[Gate]:
        if surface not in ("chat", "api"):
            raise ValueError("surface must be 'chat' or 'api'")
        if self.user is None:
            return [Gate("catalog", 0, "catalog", False)]
        if self.is_admin:
            return []
        gates = [Gate("rollout", model["id"], model["display_name"], bool(model["is_rolled_out"]))]
        if model["is_uncensored"]:
            gates.append(self.gate("uncensored", 0, access_db.SCOPE_LABELS["uncensored"]))
        if model["is_image_generation"]:
            gates.append(self.gate("image_generation", 0, access_db.SCOPE_LABELS["image_generation"]))
        for category in self.categories.get(model["id"], []):
            if category["scope"] in (surface, "both"):
                gates.append(self.gate("category", category["id"], category["name"]))
        gates.append(self.gate("model", model["id"], model["display_name"]))
        return gates

    def can_use(self, model, surface: str) -> bool:
        return model is not None and all(gate.allowed for gate in self.model_gates(model, surface))

    def denied_gates(self, model, surface: str) -> list[Gate]:
        return [gate for gate in self.model_gates(model, surface) if not gate.allowed]

    def surface_categories(self, model, surface: str) -> list:
        return [c for c in self.categories.get(model["id"], []) if c["scope"] in (surface, "both")]


def _column(model, name: str):
    try:
        return model[name]
    except (IndexError, KeyError):
        return None


def offered(model) -> bool:
    """Not withdrawn by the model lifecycle: ignored, failing, retired or being deleted (for everyone)."""
    return bool(model) and _column(model, "enrollment") != "ignored" and not _column(model, "failing_at") \
        and not _column(model, "retired_at") and not _column(model, "delete_requested_at")


def is_text_model(model) -> bool:
    return bool(model and model["backend"] in ("ollama", "claude", "external") and model["backend_available"]
                and not model["is_image_generation"] and not _column(model, "embedding_only") and offered(model))


def is_image_model(model, images_enabled: bool) -> bool:
    return bool(images_enabled and model and model["backend"] == "comfyui" and model["backend_available"]
                and model["is_image_generation"] and offered(model))


def usable_models(context: AccessContext, surface: str, *, kind: str = "text", images_enabled: bool = False,
                  unreviewed: bool = False):
    """Models the user may use on *surface*. ``kind`` is text, image or any.

    Models waiting for review (``enrollment='new'``) are left out, for administrators too, so ``auto``,
    fallbacks and model lists never pick one; *unreviewed* includes them for administrators (who may still
    name one explicitly, see ``services.inference.select_model``).
    """
    result = []
    for model in catalog.list_models(rolled_out_only=not context.is_admin):
        if _column(model, "enrollment") == "new" and not (unreviewed and context.is_admin):
            continue
        text, image = is_text_model(model), is_image_model(model, images_enabled)
        if (kind == "text" and not text) or (kind == "image" and not image) or (kind == "any" and not (text or image)):
            continue
        if context.can_use(model, surface):
            result.append(model)
    return result
