# Agent Style Notes

- Keep gateway-domain logic grouped by responsibility. Do not create a module for a single tiny helper unless it is a stable boundary.
- Prefer one domain-facing class over layers of pass-through functions. Route code should call compact APIs such as `context.request()`, `context.response(...)`, and `context.error(...)`.
- Put helper functions that are only used by one class inside that class as `@staticmethod`.
- Order class methods by role: `__init__`, public lifecycle/API methods, externally useful helpers, internal methods, then static methods.
- Leave two blank lines between methods in classes.
- Leave a blank line between nested blocks and between logical blocks inside longer methods.
- Avoid duplicated log fields. If a value is present inside a structured JSON field, do not repeat it as a top-level field unless it is needed for indexing or dashboards.
- Loki warning events must include `warn_reason`. Do not add a warning level without explaining the reason in that field.
