"""
Phase 4: build the right transport and engine from configuration.

This is the ONLY module that knows every implementation by name. Everything
else depends on the contracts in core/, which is what lets `bot.py` switch
vendors without a code change.

WHY THIS IS NOT IN core/
------------------------
A factory has to import every implementation it can build -- so if it lived in
core/, core would import Asterisk and Pipecat, and the whole point of the
layering would be gone. (There is a check that core/ imports only abc, typing
and asyncio.) The factory sits above the layers instead, next to bot.py, which
is the one place allowed to know what is actually being run.
"""

from core.config import AppConfig, ConfigError
from core.engine import Engine
from core.transport import BaseTransport


def create_transport(config: AppConfig) -> BaseTransport:
    """Build the telephony transport named by transport.provider."""
    provider = config.transport.provider

    if provider == "asterisk":
        # Imported lazily so an unused vendor's dependencies never have to be
        # installed -- and so a broken adapter can't stop the others loading.
        from transports.asterisk import AsteriskTransport

        a = config.transport.asterisk
        return AsteriskTransport(
            host=a.audiosocket_host,
            port=a.audiosocket_port,
            ari_base_url=a.ari_url,
            ari_app=a.ari_app,
            ari_user=a.ari_user,
            ari_password=a.ari_password,
            transfer_context=a.transfer_context,
            media_host=a.media_host,
        )

    raise ConfigError(
        f"transport.provider: '{provider}' is not implemented. "
        "Valid options are: ['asterisk']"
    )


def create_engine(config: AppConfig) -> Engine:
    """Build the conversation engine named by engine.provider.

    Called once PER CALL: an engine holds that call's conversation state, so
    instances are not shared between calls.
    """
    provider = config.engine.provider

    if provider == "pipecat":
        from engine.pipecat_engine import PipecatEngine

        return PipecatEngine(config.engine)

    raise ConfigError(
        f"engine.provider: '{provider}' is not implemented. "
        "Valid options are: ['pipecat']"
    )
