"""Telephony vendor adapters. One module per vendor, each implementing the
`core.transport` contracts.

  asterisk -- Asterisk / FreePBX via ARI + AudioSocket (the only one today)

A new vendor is a new module here plus a line in the Phase 4 factory. Nothing
outside this package should import a vendor module by name.
"""
