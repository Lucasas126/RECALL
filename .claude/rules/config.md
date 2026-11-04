---
paths:
  - "settings.yaml"
  - ".config/**/*.yaml"
---
# YAML configuration guidelines

- Keep framework settings (`settings.yaml`) separate from user and agent configuration under `.config/`; these have different owners and lifecycles.
- Follow the existing schema and examples in the pages in `docs/reference/` and the Pydantic models in `src/config/models.py`. Do not add a YAML option without wiring and validating it in the implementation.
- Use the existing lowercase `snake_case` keys and stable identifiers. Quote values when YAML could interpret them as another type.
- Store provider names, model IDs, and configuration values in YAML as appropriate, but never store API keys or other secrets. Use an environment-variable name for a secret reference.
- Keep paths and data scoped to the intended user and agent. Do not add shared defaults or implicit cross-agent access unless the design and validation are updated together.
- Keep examples consistent with the implemented behavior; label planned configuration features as planned rather than implying they work today.
- When changing a setting, update its model, loader/runtime use, and the pages in `docs/reference/` together. Validate loading with the project environment when practical.
