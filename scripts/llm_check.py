"""Check that the pinned local model is installed with the pinned digest and answers schema-valid JSON."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def main() -> int:
    import django

    django.setup()
    from agent.llm import ProviderError, strict_schema
    from agent.replay import load_llm_config, providers_for
    from agent.roles import RegimeDecision

    config = load_llm_config()
    try:
        provider = providers_for("live-local", config)[0]
    except ProviderError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"ok: {provider.identity.model} digest {provider.identity.digest[:12]} matches config/llm.toml")
    request = {
        "system": "Answer with the JSON object only.",
        "schema": strict_schema(RegimeDecision),
        "messages": [{"role": "user", "content": '{"markets": [], "note": "health check"}'}],
        "max_tokens": 300,
        "options": dict(config["options"]),
    }
    response = provider.create(request)
    RegimeDecision.model_validate_json(response["text"])
    print(f"ok: schema-valid answer in {response['usage'].get('seconds', 0):.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
