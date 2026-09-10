"""The control plane: a small read-only HTTP surface over the running service.

Outside `core/` for the same reason as `transports/`, `engine/` and `stores/`:
it is an implementation, and the contracts must not import their implementations.
"""
