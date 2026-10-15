# Living Codebase Brain for VS Code

Marks the methods you are changing that have been **fixed before**, so you look at the
old fix before you undo it.

When you save a Java, Kotlin, Groovy or Scala file, the extension runs
`python -m lcb.check` on the repository. That command compares your changes with the
last commit and looks up each changed method in the Living Codebase Brain code graph.
Methods linked to a past fix get an underline on their signature line, and an entry in
the **Problems** panel. A past fix is a GitHub issue resolved through a PR, or a commit
whose message says it fixed something. Hover the underline to see the fix, its
confidence, the method's callers and who changed it last.

```
OwnerController.processFindForm has been fixed before — check this change doesn't undo it.
• bb37aad by Luiz Fernando de Andrade Gadelha, 2026-07-24 (83%): "fix: normalize whitespace in owner search"
• +2 weaker links
0 callers · last changed by Luiz Fernando de Andrade Gadelha · risk rank 24 of 121
```

The status bar shows `LCB` with the number of marked methods. A disconnected icon means
the check could not run, and its tooltip says why: Neo4j stopped, not a git repository,
wrong Python. Click the status bar item, or run **Living Codebase Brain: Check changes
for past bug fixes**, to check again.

**Settings are optional.** `lcb setup <repo>` installs this extension and records the
Python, checkout and Neo4j address in `~/.lcb/config.json`, which the extension reads.
Anything you set in VS Code overrides that file.

## Requirements

- A Living Codebase Brain checkout, with the repository loaded into its graph
  (`lcb.load`, `lcb.git_ingest`, and optionally `lcb.github_ingest`).
- Neo4j running.

## Settings

| Setting | Default | |
|---|---|---|
| `lcb.projectRoot` | from `lcb setup` | Path to the checkout (the folder containing `lcb/`). |
| `lcb.pythonPath` | from `lcb setup` | Python to run with. Else the checkout's `.venv-mac` or `.venv`, else `python3`. |
| `lcb.repo` | workspace folders | Repository to check, if it is not the folder you opened. |
| `lcb.minConfidence` | `0.2` | How strong a link to a past fix must be before a method is marked. |
| `lcb.severity` | `warning` | `error` for a red underline, `warning` for yellow, `information` for blue. |
| `lcb.neo4jUri` | `NEO4J_URI` or `bolt://localhost:7687` | Code graph address. The password comes from `NEO4J_PASSWORD`. |

## Build, test, install

```bash
npm install
npm test          # unit tests, plus two runs of the real lcb.check
npm run package   # -> living-codebase-brain-0.1.0.vsix
```

Install the `.vsix` with **Extensions → … → Install from VSIX…**, or run
`code --install-extension living-codebase-brain-0.1.0.vsix`.
