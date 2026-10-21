"""Guardian: an operations agent that watches Argus and prepares fixes, but never ships them.

What it does, in order of increasing power (the `mode` setting):

    off    nothing runs
    watch  checks the server, its logs, and the test suite on the main branch; asks a read-only
           model to diagnose each problem; reports it
    ask    as `watch`, and when a problem looks fixable it proposes a fix; a person approves it
    fix    as `ask`, but prepares the fix without waiting for approval

A fix is always a draft pull request from a `guardian/...` branch. The guardian has no way to
merge, deploy, or touch the release channel: those steps stay with a person.

The design rests on one idea: the model is allowed to be wrong, so everything it can do is
bounded by code it cannot talk its way around.

* The model works through a small set of tools implemented here (`tools.py`), not through a
  shell. Every path goes through `policy.py`, which refuses secrets and the code that moves
  money, signs releases, or handles accounts.
* Text that comes from outside (logs, test output) is untrusted: it is redacted, delimited, and
  the model is told it is data. Even if it is fooled, the tools it holds cannot do harm.
* A fix must pass a second, independent gate that does not involve the model at all: the diff
  is checked against the policy and the full test suite is run.
* Spending is capped per run and per day, every action goes to an append-only audit log, and
  a file named `OFF` in the guardian's home folder stops everything at once.

Run it with `python -m argus.guardian` (see `docs/guardian.md`). It is not part of the desktop
app and is deliberately not reachable from `argus.cli`, so it never ends up in a build.
"""
