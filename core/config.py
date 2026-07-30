"""
Phase 4: the typed configuration loader.

Reads config.yaml, validates it, resolves environment-variable references, and
hands back frozen dataclasses. Everything downstream reads attributes, not
dictionaries -- so a typo becomes an error here, at startup, instead of an
AttributeError mid-call or a setting that silently does nothing.

THE SECRETS RULE
----------------
config.yaml never contains a secret. It contains the NAME of the environment
variable holding one:

    api_key_env: DEEPGRAM_API_KEY      # the name -- safe to commit
    api_key: dg_live_abc123            # NEVER do this

That makes config.yaml safe to commit, diff and paste; real keys stay in .env,
which is git-ignored.

FAIL FAST, AND SAY WHY
----------------------
Three kinds of mistake are caught at startup, each naming the exact dotted path:
  * an unknown key            -- `engine.tts.voicce` is a typo, not a new feature
  * a missing required key
  * a missing environment variable that config.yaml referenced

The unknown-key check matters more than it looks: without it, a misspelled key
is silently ignored, and "I changed the config and nothing happened" is a
genuinely horrible thing to debug.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised for any invalid configuration. The message is meant for a human."""


# ---------------------------------------------------------------------------
# Small validation helpers. Every message includes the dotted path, so the user
# is told exactly which line of their YAML to look at.
# ---------------------------------------------------------------------------
def _section(data: dict, path: str, *, allowed: set[str], required: set[str] = frozenset()) -> dict:
    """Validate one mapping: right type, no unknown keys, no missing keys."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a block of settings, got {type(data).__name__}")

    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(
            f"{path}: unknown setting(s) {sorted(unknown)}. "
            f"Valid settings here are: {sorted(allowed)}"
        )
    missing = required - set(data)
    if missing:
        raise ConfigError(f"{path}: missing required setting(s) {sorted(missing)}")
    return data


def _choice(value, path: str, valid: set[str]) -> str:
    if value not in valid:
        raise ConfigError(
            f"{path}: '{value}' is not supported. Valid options are: {sorted(valid)}"
        )
    return value


def _number(value, path: str, *, kind=float):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}: expected a number, got {value!r}")
    return kind(value)


class _Env:
    """Resolves *_env references, collecting ALL misses before complaining.

    Reporting every missing variable at once beats making someone restart four
    times to discover four names.
    """

    def __init__(self):
        self.missing: list[str] = []

    def get(self, var_name: str, path: str) -> str:
        if not isinstance(var_name, str) or not var_name:
            raise ConfigError(f"{path}: expected the NAME of an environment variable")
        value = os.getenv(var_name)
        if not value:
            self.missing.append(f"{var_name}  (referenced by {path})")
            return ""
        return value

    def raise_if_missing(self):
        if self.missing:
            raise ConfigError(
                "These environment variables are named in config.yaml but are not set:\n  "
                + "\n  ".join(self.missing)
                + "\n\nSet them in your .env file (never put the values in config.yaml)."
            )


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AsteriskConfig:
    ari_url: str = "http://localhost:8088"
    ari_app: str = "voiceagent"
    ari_user: str = ""
    ari_password: str | None = None  # None = ARI deliberately disabled
    audiosocket_host: str = "0.0.0.0"
    audiosocket_port: int = 8090
    media_host: str = "127.0.0.1"
    transfer_context: str = "transfer"


@dataclass(frozen=True)
class TransportConfig:
    provider: str = "asterisk"
    asterisk: AsteriskConfig = field(default_factory=AsteriskConfig)


VALID_TRANSPORTS = {"asterisk"}


def _load_asterisk(data: dict, env: _Env, path: str) -> AsteriskConfig:
    d = _section(
        data,
        path,
        allowed={
            "ari_url",
            "ari_app",
            "ari_user_env",
            "ari_pass_env",
            "audiosocket_host",
            "audiosocket_port",
            "media_host",
            "transfer_context",
        },
    )
    defaults = AsteriskConfig()

    # ARI is optional: omit ari_pass_env (or set it to nothing) to run without
    # call control -- audio and conversation work, transfer does not. But if you
    # DO name a variable, it must exist; silently degrading to "no transfer"
    # because a password was unset is exactly the footgun we are removing.
    pass_env = d.get("ari_pass_env")
    if pass_env:
        ari_password = env.get(pass_env, f"{path}.ari_pass_env")
        ari_user = env.get(d.get("ari_user_env", "ARI_USER"), f"{path}.ari_user_env")
    else:
        ari_password = None
        ari_user = ""

    return AsteriskConfig(
        ari_url=d.get("ari_url", defaults.ari_url),
        ari_app=d.get("ari_app", defaults.ari_app),
        ari_user=ari_user,
        ari_password=ari_password,
        audiosocket_host=d.get("audiosocket_host", defaults.audiosocket_host),
        audiosocket_port=int(d.get("audiosocket_port", defaults.audiosocket_port)),
        media_host=d.get("media_host", defaults.media_host),
        transfer_context=d.get("transfer_context", defaults.transfer_context),
    )


def _load_transport(data: dict, env: _Env) -> TransportConfig:
    d = _section(data, "transport", allowed={"provider"} | VALID_TRANSPORTS, required={"provider"})
    provider = _choice(d["provider"], "transport.provider", VALID_TRANSPORTS)
    return TransportConfig(
        provider=provider,
        asterisk=_load_asterisk(d.get("asterisk", {}), env, "transport.asterisk"),
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
# Which providers each slot understands. Adding one means installing the Pipecat
# extra, adding the name here, and adding a branch in the engine's builder.
VALID_STT = {"deepgram"}
VALID_LLM = {"google"}
VALID_TTS = {"deepgram"}
VALID_ENGINES = {"pipecat"}
VALID_VAD = {"silero"}


@dataclass(frozen=True)
class STTConfig:
    provider: str = "deepgram"
    api_key: str = ""
    sample_rate: int = 8000
    model: str | None = None


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "google"
    model: str = "gemini-flash-lite-latest"
    api_key: str = ""


@dataclass(frozen=True)
class TTSConfig:
    provider: str = "deepgram"
    voice: str = "aura-2-helena-en"
    api_key: str = ""
    sample_rate: int = 8000


@dataclass(frozen=True)
class TurnTakingConfig:
    vad: str = "silero"
    silence_timeout_s: float = 0.6
    smart_turn_v3: bool = False


@dataclass(frozen=True)
class PersonaConfig:
    name: str = "Alex"
    company: str = "Techbridge"
    system_prompt: str = ""
    greeting: str = ""


@dataclass(frozen=True)
class EngineConfig:
    provider: str = "pipecat"
    stt: STTConfig = field(default_factory=STTConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    turn_taking: TurnTakingConfig = field(default_factory=TurnTakingConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    idle_timeout_s: int = 30
    transfer_announce_s: float = 3.0


def _load_stt(data: dict, env: _Env) -> STTConfig:
    path = "engine.stt"
    d = _section(
        data, path, allowed={"provider", "api_key_env", "sample_rate", "model"},
        required={"provider", "api_key_env"},
    )
    return STTConfig(
        provider=_choice(d["provider"], f"{path}.provider", VALID_STT),
        api_key=env.get(d["api_key_env"], f"{path}.api_key_env"),
        sample_rate=int(d.get("sample_rate", 8000)),
        model=d.get("model"),
    )


def _load_llm(data: dict, env: _Env) -> LLMConfig:
    path = "engine.llm"
    d = _section(
        data, path, allowed={"provider", "model", "api_key_env"},
        required={"provider", "model", "api_key_env"},
    )
    return LLMConfig(
        provider=_choice(d["provider"], f"{path}.provider", VALID_LLM),
        model=d["model"],
        api_key=env.get(d["api_key_env"], f"{path}.api_key_env"),
    )


def _load_tts(data: dict, env: _Env) -> TTSConfig:
    path = "engine.tts"
    d = _section(
        data, path, allowed={"provider", "voice", "api_key_env", "sample_rate"},
        required={"provider", "voice", "api_key_env"},
    )
    return TTSConfig(
        provider=_choice(d["provider"], f"{path}.provider", VALID_TTS),
        voice=d["voice"],
        api_key=env.get(d["api_key_env"], f"{path}.api_key_env"),
        sample_rate=int(d.get("sample_rate", 8000)),
    )


def _load_turn_taking(data: dict) -> TurnTakingConfig:
    path = "engine.turn_taking"
    d = _section(data, path, allowed={"vad", "silence_timeout_s", "smart_turn_v3"})
    defaults = TurnTakingConfig()
    timeout = _number(d.get("silence_timeout_s", defaults.silence_timeout_s), f"{path}.silence_timeout_s")
    if not 0.05 <= timeout <= 10:
        raise ConfigError(
            f"{path}.silence_timeout_s: {timeout} is out of range. Use roughly 0.3-2.0 "
            "seconds; below that the agent interrupts constantly, above it feels dead."
        )
    return TurnTakingConfig(
        vad=_choice(d.get("vad", defaults.vad), f"{path}.vad", VALID_VAD),
        silence_timeout_s=timeout,
        smart_turn_v3=bool(d.get("smart_turn_v3", defaults.smart_turn_v3)),
    )


def _resolve_prompt(d: dict, path: str, base_dir: Path, name: str, company: str) -> str:
    """Read a system prompt from `system_prompt` or `system_prompt_file`.

    Shared by the single persona under engine: and by every persona in the pool,
    so both get the same rules and the same error messages.
    """
    if "system_prompt" in d and "system_prompt_file" in d:
        raise ConfigError(
            f"{path}: set either system_prompt (inline) or system_prompt_file, not both"
        )
    if "system_prompt_file" in d:
        prompt_path = (base_dir / d["system_prompt_file"]).resolve()
        try:
            prompt = prompt_path.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigError(
                f"{path}.system_prompt_file: could not read {prompt_path} ({e})"
            ) from e
    elif "system_prompt" in d:
        prompt = str(d["system_prompt"])
    else:
        raise ConfigError(f"{path}: needs either system_prompt or system_prompt_file")

    if not prompt.strip():
        raise ConfigError(f"{path}: the system prompt is empty")

    # Placeholders, so the same prompt file works for any persona. str.replace
    # rather than str.format: a stray brace in prompt text must not explode.
    return prompt.replace("{name}", name).replace("{company}", company)


def _load_persona(data: dict, base_dir: Path) -> PersonaConfig:
    path = "engine.persona"
    d = _section(
        data, path,
        allowed={"name", "company", "system_prompt", "system_prompt_file", "greeting"},
    )
    defaults = PersonaConfig()
    name = d.get("name", defaults.name)
    company = d.get("company", defaults.company)
    prompt = _resolve_prompt(d, path, base_dir, name, company)
    greeting = d.get(
        "greeting", f"Hi, this is {name} at {company}. How can I help you today?"
    )
    return PersonaConfig(name=name, company=company, system_prompt=prompt, greeting=greeting)


def _load_engine(data: dict, env: _Env, base_dir: Path) -> EngineConfig:
    d = _section(
        data,
        "engine",
        allowed={
            "provider", "stt", "llm", "tts", "turn_taking", "persona",
            "idle_timeout_s", "transfer_announce_s",
        },
        required={"provider", "stt", "llm", "tts"},
    )
    defaults = EngineConfig()
    return EngineConfig(
        provider=_choice(d["provider"], "engine.provider", VALID_ENGINES),
        stt=_load_stt(d["stt"], env),
        llm=_load_llm(d["llm"], env),
        tts=_load_tts(d["tts"], env),
        turn_taking=_load_turn_taking(d.get("turn_taking", {})),
        persona=_load_persona(d.get("persona", {}), base_dir),
        idle_timeout_s=int(d.get("idle_timeout_s", defaults.idle_timeout_s)),
        transfer_announce_s=_number(
            d.get("transfer_announce_s", defaults.transfer_announce_s),
            "engine.transfer_announce_s",
        ),
    )


# ---------------------------------------------------------------------------
# Pool -- the roster of personas the service can hand to callers
# ---------------------------------------------------------------------------
# A persona is not a new kind of object: it is the handful of engine settings
# that differ between agents (who they are, how they sound, what they were
# told). Everything else -- providers, models, keys, turn-taking -- is shared,
# so adding an agent is three lines of YAML and a prompt file, not a code change.
#
# CAPACITY LIVES HERE. N is len(personas): the roster IS the capacity, so the
# two can never disagree. (The Asterisk dialplan cap is a separate number that
# must be kept equal by hand -- Phase 4 -- because the dialplan cannot read
# this file.)
@dataclass(frozen=True)
class PoolPersona:
    """One agent in the pool. Frozen, and shared read-only by every call.

    This holds no conversation state on purpose: it is a description, not a
    running agent. The per-call engine built FROM it holds all the state, and
    is thrown away when the call ends -- which is what stops one caller's
    conversation leaking into the next caller's.
    """

    name: str
    voice: str
    company: str = "Techbridge"
    system_prompt: str = ""
    greeting: str = ""


@dataclass(frozen=True)
class PoolConfig:
    personas: tuple[PoolPersona, ...] = ()

    @property
    def capacity(self) -> int:
        """Maximum simultaneous calls: one persona each, so N = the roster size."""
        return len(self.personas)

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.personas]


def _text(value, path: str) -> str:
    """A setting that must be a non-blank string. Catches `voice:` with nothing
    after it, which YAML happily parses as None."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: expected a non-empty value, got {value!r}")
    return value.strip()


def _load_pool(data, engine: EngineConfig, base_dir: Path) -> PoolConfig:
    """Validate the persona roster.

    Every check here exists so a misconfigured service refuses to START, rather
    than answering a call and failing halfway through it -- by which point a
    real caller is already listening to silence.
    """
    path = "pool"
    d = _section(data, path, allowed={"personas"}, required={"personas"})

    raw = d["personas"]
    if not isinstance(raw, list):
        raise ConfigError(f"{path}.personas: expected a list of personas")
    if not raw:
        raise ConfigError(
            f"{path}.personas: the pool is empty, so no call could ever be answered. "
            "List at least one persona."
        )

    personas: list[PoolPersona] = []
    seen: dict[str, str] = {}  # lowercased name -> name as written

    for i, entry in enumerate(raw):
        item = f"{path}.personas[{i}]"
        e = _section(
            entry, item,
            allowed={"name", "voice", "company", "system_prompt", "system_prompt_file", "greeting"},
            required={"name", "voice"},
        )

        name = _text(e["name"], f"{item}.name")
        # Names are how a persona is identified in logs and in the busy/assign
        # accounting, so two agents called "Sarah" would make those unreadable.
        # Compared case-insensitively: "sarah" and "Sarah" are the same person
        # to everyone reading a log line.
        key = name.lower()
        if key in seen:
            raise ConfigError(
                f"{item}.name: '{name}' duplicates '{seen[key]}' earlier in the pool. "
                "Persona names must be unique -- they identify the agent in logs."
            )
        seen[key] = name

        voice = _text(e["voice"], f"{item}.voice")
        company = e.get("company", engine.persona.company)
        personas.append(
            PoolPersona(
                name=name,
                voice=voice,
                company=company,
                system_prompt=_resolve_prompt(e, item, base_dir, name, company),
                greeting=e.get(
                    "greeting", f"Hi, this is {name} at {company}. How can I help you today?"
                ),
            )
        )

    return PoolConfig(personas=tuple(personas))


def _pool_from_single_persona(engine: EngineConfig) -> PoolConfig:
    """No `pool:` block -> a pool of one, built from engine.persona.

    This is the regression guard, made structural: an old config file that never
    heard of pools still describes exactly one agent with exactly today's voice
    and prompt, so it keeps behaving exactly as it did.
    """
    p = engine.persona
    return PoolConfig(
        personas=(
            PoolPersona(
                name=p.name,
                voice=engine.tts.voice,
                company=p.company,
                system_prompt=p.system_prompt,
                greeting=p.greeting,
            ),
        )
    )


def config_for_persona(config: AppConfig, persona: PoolPersona) -> AppConfig:
    """The shared config, with one persona's differences applied.

    Returns a NEW AppConfig -- the dataclasses are frozen, so nothing is mutated
    and two calls preparing two personas at the same moment cannot interfere.
    Feed the result to create_engine() and you get that persona's engine, built
    by the one factory that already exists; there is no second engine path.
    """
    engine = replace(
        config.engine,
        tts=replace(config.engine.tts, voice=persona.voice),
        persona=PersonaConfig(
            name=persona.name,
            company=persona.company,
            system_prompt=persona.system_prompt,
            greeting=persona.greeting,
        ),
    )
    return replace(config, engine=engine)


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AppConfig:
    transport: TransportConfig
    engine: EngineConfig
    pool: PoolConfig = field(default_factory=PoolConfig)
    source: Path | None = None


DEFAULT_CONFIG_NAME = "config.yaml"


def config_path(explicit: str | None = None) -> Path:
    """Where to read config from: an explicit path, $VOICEAGENT_CONFIG, or the
    config.yaml sitting next to the code."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    from_env = os.getenv("VOICEAGENT_CONFIG")
    if from_env:
        return Path(from_env).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_NAME).resolve()


def load_config(path: str | Path | None = None) -> AppConfig:
    """Read, validate and return the configuration. Raises ConfigError on any
    problem, with a message meant to be read by whoever edited the file."""
    # Load .env here rather than relying on the caller having done it: resolving
    # *_env references is this module's job, so it should not depend on a hidden
    # ordering requirement that any new entry point would have to remember.
    # (load_dotenv does not override variables already set in the real
    # environment, so a deployment that exports them properly still wins.)
    load_dotenv()

    p = config_path(str(path) if path else None)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(
            f"No config file at {p}. Copy config.yaml from the repo, or point "
            "VOICEAGENT_CONFIG at one."
        ) from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{p} is not valid YAML: {e}") from e

    top = _section(
        raw, "(top level)",
        allowed={"transport", "engine", "pool"}, required={"transport", "engine"},
    )

    env = _Env()
    transport = _load_transport(top["transport"], env)
    engine = _load_engine(top["engine"], env, base_dir=p.parent)
    # Pool after engine: personas inherit shared settings (the company name) from
    # it. Omitting `pool:` is legal and means "one agent" -- see
    # _pool_from_single_persona.
    pool = (
        _load_pool(top["pool"], engine, base_dir=p.parent)
        if "pool" in top
        else _pool_from_single_persona(engine)
    )
    # Everything else validated first, so one run reports every problem it can.
    env.raise_if_missing()

    return AppConfig(transport=transport, engine=engine, pool=pool, source=p)
