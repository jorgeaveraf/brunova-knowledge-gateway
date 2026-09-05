import json
from pathlib import Path


def test_agent_signal_noncurrent_cleanup_is_prefix_scoped_and_never_targets_live_objects():
    policy = json.loads(
        Path("infra/agent-signals-lifecycle.json").read_text(encoding="utf-8")
    )
    assert policy == {
        "rule": [
            {
                "action": {"type": "Delete"},
                "condition": {
                    "daysSinceNoncurrentTime": 1,
                    "isLive": False,
                    "matchesPrefix": ["agent-signals/items/"],
                },
            }
        ]
    }
