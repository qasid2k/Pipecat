# Personas

The roster of agents the service can hand to callers. **The roster is the
capacity**: N simultaneous calls = N personas listed in `config.yaml` under
`pool.personas`.

Related: [[architecture]], [[decisions]], [[runbook]], [[changelog]].

---

## The roster

| Name | Voice (Deepgram Aura-2) | Personality | Prompt file |
|---|---|---|---|
| Alex | `aura-2-helena-en` | Warm, calm, professional. Friendly but not chatty. The original single-agent persona — unchanged. | `prompts/alex.txt` |
| Sarah | `aura-2-thalia-en` | Brisk, efficient, upbeat. Leads with the answer, adds detail only if asked. | `prompts/sarah.txt` |
| Daniel | `aura-2-orion-en` | Easy-going, patient, reassuring. Slows down for flustered or unsure callers. | `prompts/daniel.txt` |

**N = 3.** Defined in exactly one place in the app: the length of that list.

`company` (Techbridge) and the greeting are inherited from `engine.persona`
unless a persona overrides them, so all three introduce themselves the same way
with their own name.

---

## What a persona is

A persona is **not** a running agent or a new kind of object. It is the small
set of engine settings that differ between agents:

    name  +  voice  +  system prompt

Everything else — STT/LLM/TTS providers, models, API keys, turn-taking, idle
timeout — is shared, in `engine:`. Building a persona's engine means calling the
*existing* `create_engine()` with those three values overridden
(`config_for_persona()` in [core/config.py](../core/config.py), wrapped by
`create_engine_for_persona()` in [factories.py](../factories.py)). There is no
second engine path and nothing persona-aware inside the engine.

`PoolPersona` is a frozen dataclass holding only that description. It carries no
conversation state, which is why it is safe to share read-only across calls and
to hand to a later caller: the *state* lives in the per-call engine, which is
built fresh and thrown away when the call ends.

---

## Adding or removing a persona

1. Write `prompts/<name>.txt`. Use `{name}` and `{company}` as placeholders —
   they are substituted per persona, so a prompt file is not hard-wired to one
   agent.
2. Add three lines under `pool.personas` in `config.yaml`: `name`, `voice`,
   `system_prompt_file`.
3. **Update the Asterisk dialplan cap to match the new N** (Phase 4). The
   dialplan cannot read `config.yaml`, so this number is kept equal by hand. If
   the dialplan cap is higher, the extra caller reaches the app and gets the
   blunt app-side reject instead of the spoken busy message. See [[runbook]].
4. Restart the bot. Config is read once at startup; there is no live reload.
5. Check provider concurrency before raising N — all personas share one Deepgram
   key and one Gemini key, so N calls = N concurrent streams on each. See
   [[runbook]].

Pick voices that are **clearly distinguishable**. Two agents who sound alike
defeat the point of the pool from the caller's side.

---

## Prompt files are deliberately duplicated

`sarah.txt` and `daniel.txt` repeat the phone-call rules and transfer
instructions from `alex.txt` verbatim; only IDENTITY/TONE and a line or two
elsewhere differ.

That is a conscious trade for now: a shared base file plus per-persona fragments
would mean a new config mechanism (an include, or a prompt-composition step) for
three files. If the roster grows past a handful, or a rule has to be corrected
in every file at once, revisit it — that is the point at which the duplication
starts costing more than the mechanism would.

---

## Validation (fails at startup, not mid-call)

A misconfigured roster refuses to start. Each message names the exact persona
index and setting. Rejected:

- empty `personas` list (nothing could ever answer)
- duplicate names, compared case-insensitively (`Alex` vs `ALEX`) — names
  identify the agent in logs, so they must be unambiguous
- missing or blank `name` / `voice`
- a `system_prompt_file` that cannot be read
- a prompt that is empty or whitespace
- both `system_prompt` and `system_prompt_file` set, or neither
- any unknown key (`voicce:` is a typo, not a new feature)

Persona entries reference no environment variables of their own — the API keys
their engines use are the shared ones under `engine:`, already validated by the
same startup pass. So "a persona referencing an unset variable" is caught, just
one level up.

**Not validated:** whether a voice name actually exists at Deepgram. A typo
there surfaces as a TTS failure on the first call using that persona, not at
startup. Fix is a one-line `config.yaml` edit and a bot restart.

---

## Regression guard

Two ways to get exactly the old single-agent behaviour:

- a `pool.personas` list containing only Alex, or
- **no `pool:` block at all** — the loader then synthesises a pool of one from
  `engine.persona` + `engine.tts.voice`.

Both are verified to produce the same voice, prompt and greeting as before
pooling existed, so an older `config.yaml` keeps working untouched.
