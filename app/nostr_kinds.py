"""Experimental Meerat Nostr event schema v2 constants.

Addressable state uses 3xxxx kinds with a stable ``d`` tag. Immutable facts use
regular kinds so relay history cannot be replaced accidentally.
"""

SCHEMA_VERSION = "2"

CAMPAIGN_KIND = 39001
ENROLLMENT_KIND = 39002
CONVERSION_KIND = 2801
PAYOUT_KIND = 2802
REVERSAL_KIND = 2803

ADDRESSABLE_KINDS = {CAMPAIGN_KIND, ENROLLMENT_KIND}
IMMUTABLE_KINDS = {CONVERSION_KIND, PAYOUT_KIND, REVERSAL_KIND}

CAMPAIGN_STATUSES = {"active", "paused", "ended"}
ENROLLMENT_STATUSES = {"pending", "approved", "rejected", "terminated"}
REVERSAL_REASONS = {"refund", "fraud", "chargeback", "cancelled", "other"}
