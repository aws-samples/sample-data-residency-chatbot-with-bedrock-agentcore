"""Data-residency guard for Bedrock model selection (Requirement 1.6, 1.7).

WHY this exists
---------------
MNRE is an MHA-adjacent engagement with strict data-residency obligations: no
program data may leave India, and every inference call must stay in region
``ap-south-1`` (Mumbai).

Amazon Bedrock cross-region inference profiles violate this:
  - ``apac.*`` profiles load-balance inference across the whole Asia-Pacific
    region group (Singapore, Tokyo, etc.) — data can leave India.
  - ``global.*`` profiles route worldwide — data can leave India.

A *bare* modelId (e.g. ``mistral.mistral-large-2402-v1:0``) invoked ON_DEMAND
against a region-pinned ``bedrock-runtime`` client stays entirely in-region.

This guard fails fast at Agent_Lambda startup if anyone configures a
cross-region inference profile, preventing a residency violation from ever
reaching Bedrock.
"""

# In-region ON_DEMAND model for MNRE: Anthropic Claude 3 Haiku — invocable in
# ap-south-1 with a bare modelId and reliable native tool/function calling. The
# residency guard (no apac./global. prefix) still applies. Mistral Large is also
# in-region ON_DEMAND but proved unreliable at issuing tool calls (it narrates
# the call as text instead of invoking), so Claude 3 Haiku is the default.
DEFAULT_MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"

# Cross-region inference profile prefixes that can route inference outside India.
_CROSS_REGION_PREFIXES = ("apac.", "global.")


def residency_guard(model_id: str) -> str:
    """Return ``model_id`` if it is an in-region bare modelId, else raise.

    Args:
        model_id: The Bedrock model identifier to validate.

    Returns:
        The same ``model_id`` when it is safe to use in-region.

    Raises:
        ValueError: If ``model_id`` starts with ``apac.`` or ``global.``,
            i.e. a cross-region inference profile that can move data/inference
            outside India.
    """
    if model_id.startswith(_CROSS_REGION_PREFIXES):
        raise ValueError(
            f"Cross-region inference profile '{model_id}' is not allowed: it can "
            "route inference outside India. Use a bare in-region ON_DEMAND modelId "
            f"such as '{DEFAULT_MODEL_ID}'."
        )
    return model_id
