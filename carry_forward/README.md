# Carry Forward

Continue Hermes agent work across sessions without handoff files.

## How It Works

Hermes records every conversation in `~/.hermes/state.db` (SQLite). Carry Forward reads this database to extract what the last session was doing, then the agent picks up from there.

No handoff files. No state machines. The conversation history IS the handoff.

```
┌─────────────┐     cronjob      ┌─────────────┐     cronjob      ┌─────────────┐
│  Session 1  │ ────(1 min)───▶  │  Session 2  │ ────(1 min)───▶  │  Session 3  │
│  Does work  │                  │  Reads DB   │                  │  Reads DB   │
│  Ends       │                  │  Continues  │                  │  Stops      │
└──────┬──────┘                  └──────┬──────┘                  └─────────────┘
       │                                │
       ▼                                ▼
┌──────────────────────────────────────────────┐
│           ~/.hermes/state.db                 │
│                                              │
│  The source of truth. No handoff files.      │
└──────────────────────────────────────────────┘
```

## Files

```
carry_forward/
├── AI_GUIDE.md          # Instructions for AI agents (the main document)
├── README.md            # This file (human-oriented overview)
└── carry_forward.py     # Helper script that reads the session DB
```

## Usage

**From a live session, fire a carry-forward:**
```
cronjob(
  action='create',
  name='carry-forward',
  schedule='1m',
  repeat=1,
  prompt='Read /home/jericho/zion/projects/carry_forward/carry_forward/AI_GUIDE.md and follow its instructions.',
  skills=['carry-forward']
)
```

**Helper script commands:**
```
python3 carry_forward.py context                # Auto-extract from last session
python3 carry_forward.py last                   # List recent sessions
python3 carry_forward.py messages SESSION_ID    # Read a session's messages
python3 carry_forward.py last-id                # Print last session ID
```

## Skill Location

The Hermes skill lives at `~/.hermes/skills/devops/carry-forward/SKILL.md` and points to the AI_GUIDE.md in this project.

## When to Use

- **You're at the keyboard:** Just keep the session open. Hermes has auto context compression and handles long sessions natively (max_turns=200, gateway_timeout=2hr).
- **You want unattended work (overnight, etc.):** Use carry-forward. Fire a cron, the chain keeps going.

## Related

- Session Relay (`~/zion/projects/session_relay/`) -- older approach with handoff files. Superseded by carry-forward.
- Ralph (`~/zion/projects/session_relay/session_relay/apps/ralph-main/`) -- PRD-driven loop for external agents (Amp, Claude Code). Different use case.
