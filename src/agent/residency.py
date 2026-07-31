"""Data-residency guard for Bedrock model selection (Requirement 1.6, 1.7).

WHY this exists
---------------
This workload has strict single-Region data-residency requirements: all data
and every inference call must stay in the deployment Region (``ap-south-1``,
Mumbai).

Amazon Bedrock cross-region inference profiles violate this:
  - Geographic profiles (``us.*``, ``eu.*``, ``ap.*``, ``apac.*``, ``jp.*``,
    ``au.*``) load-balance inference across a region group — inference can
    execute outside the target Region.
  - ``global.*`` profiles route worldwide.

A *bare* modelId invoked ON_DEMAND against a region-pinned ``bedrock-runtime``
client stays entirely in-region. This solution is MODEL-AGNOSTIC: any bare
in-region modelId with Converse tool-use support works. Consult the Bedrock
regional model availability page to pick one for your target Region —
availability changes over time:
https://docs.aws.amazon.com/bedrock/latest/userguide/models-region-compatibility.html

This guard validates the ID format regardless of which foundation model is
configured. It fails fast at Agent_Lambda startup if anyone configures a
cross-region inference profile, preventing a residency violation from ever
reaching Bedrock.
"""

# Cross-region inference-profile prefixes that can route inference outside the
# target Region. Geographic prefixes + global.
_CROSS_REGION_PREFIXES = ("us.", "eu.", "ap.", "apac.", "jp.", "au.", "global.")


def residency_guard(model_id: str) -> str:
    """Return ``model_id`` if it is an in-region bare modelId, else raise.

    Args:
        model_id: The Bedrock model identifier to validate.

    Returns:
        The same ``model_id`` when it is safe to use in-region.

    Raises:
        ValueError: If ``model_id`` starts with a cross-region inference-profile
            prefix (``us.``, ``eu.``, ``ap.``, ``apac.``, ``jp.``, ``au.``,
            ``global.``), i.e. a profile that can move data/inference outside
            the target Region.
    """
    if model_id.startswith(_CROSS_REGION_PREFIXES):
        raise ValueError(
            f"Cross-region inference profile '{model_id}' is not allowed: it can "
            "route inference outside the target Region. Use a bare in-region "
            "ON_DEMAND modelId (see the Bedrock regional model availability page: "
            "https://docs.aws.amazon.com/bedrock/latest/userguide/"
            "models-region-compatibility.html)."
        )
    return model_id
