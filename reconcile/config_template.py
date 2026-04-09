"""
Configuration template. Copy to config_local.py and edit for your project.

Every field is documented. Change values, not structure.
"""

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

WS_URL = ""                      # board tool WebSocket endpoint (wss://...)
GIT_REPO = ""                    # path to git repo (e.g. "data/repo")
DISCORD_TOKEN = ""           # from .env (never commit this)
DISCORD_CHANNELS = []        # channel IDs to poll
EMAIL_DIR = "statusreports/"

# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------

MEMBER_MAP = {
    # board tool user_id → display label
    # "1001": "Alice",
    # "1002": "Bob",
}

GIT_AUTHOR_MAP = {
    # git author name → canonical name
    # "J. Doe": "Jane Doe",
    # "jdoe": "Jane Doe",
}

PM_USER_ID = ""

# ---------------------------------------------------------------------------
# Bus behavior
# ---------------------------------------------------------------------------

SWEEP_ON_ALERT = True        # historical sweep when any detector fires
SWEEP_INTERVAL = 86400       # seconds between scheduled sweeps (86400 = daily, None = disabled)

# ---------------------------------------------------------------------------
# Detector thresholds
# ---------------------------------------------------------------------------

DETECTORS = {
    "zero-commit-complete": {
        "enabled": True,
    },
    "branch-delete-before-complete": {
        "enabled": True,
        "window_seconds": 300,
    },
    "batch-completion": {
        "enabled": True,
        "window_seconds": 60,
        "min_cards": 3,
    },
    "file-reattribution": {
        "enabled": True,
    },
}

# ---------------------------------------------------------------------------
# Ingestor intervals (seconds)
# ---------------------------------------------------------------------------

GIT_POLL_INTERVAL = 60
DISCORD_POLL_INTERVAL = 120

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

OUTPUTS = {
    "console": {"enabled": True},
    "json_file": {"enabled": True, "path": "audit-output/live-alerts.jsonl"},
}

# ---------------------------------------------------------------------------
# Provisioning (Discord server auto-creation)
# ---------------------------------------------------------------------------

DISCORD_TEMPLATE_ID = ""                                     # optional guild template
DISCORD_DEFAULT_CHANNELS = ["general", "standup", "sprint-log"]
DISCORD_DEFAULT_ROLES = ["pm", "developer", "ta"]

# ---------------------------------------------------------------------------
# Scoring model (used by historical analyzer)
# ---------------------------------------------------------------------------

SCORING = {
    "perpetrator_multiplier": 2,
    "victim_multiplier": 2,
    "staleness_divisor": 7,
    "staleness_cap": 4,
    "tier_boundaries": [3, 6, 9],       # elevated, suspect, critical
    "pair_escalation_divisor": 3,
    "pair_escalation_cap": 4,
    "concentration_thresholds": [0.15, 0.30],
}
