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

from core.config import AppConfig, ConfigError, PoolPersona, config_for_persona
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


def create_engine_for_persona(config: AppConfig, persona: PoolPersona) -> Engine:
    """Build a fresh engine wearing one persona's name, voice and prompt.

    Deliberately a thin wrapper over create_engine rather than a second way to
    build an engine: a persona is just an override of a few engine settings, so
    it goes through the same factory and gets the same behaviour for free.

    Called once PER CALL, never cached. The returned engine owns that call's own
    VAD, its own provider connections and its own conversation context, so two
    concurrent calls -- even two calls on the SAME persona, if the pool ever
    allowed it -- cannot hear or remember each other.
    """
    return create_engine(config_for_persona(config, persona))
