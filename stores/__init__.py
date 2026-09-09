"""Call-record stores. Implementations of `core.records.CallStore`.

Kept out of `core/` for the same reason as `transports/` and `engine/`: a
contract that imports its own implementations is not a contract. `factories.py`
is the one module allowed to know these by name.
"""
