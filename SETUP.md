# Setting up Living Codebase Brain on a repository

How to point this project at any Java repository (Maven or Gradle) and use it from
Claude Code, VS Code and git. For how it works, see [ARCHITECTURE.md](ARCHITECTURE.md).

Commands below use two shortcuts. Set them once per terminal window:

```bash
LCB="/path/to/Living Codebase brain"       # this project's folder
PY="$LCB/.venv/bin/python"                 # its Python (on the original Mac: .venv-mac)
```

---

## Part A — First time on a machine (once)

Skip this if the machine already runs the project.

**1. Install the tools** (macOS with Homebrew shown; any install of these works)

```bash
brew install openjdk@21 neo4j python@3.13 maven git
brew install gh              # optional: GitHub issues and PRs
brew install node            # optional: only to rebuild the VS Code extension
```

**2. Start Neo4j and set its password** (the password step only before the first start)

```bash
neo4j-admin dbms set-initial-password lcbpassword
brew services start neo4j
```

**3. Get this project and install it**

```bash
git clone <this project's URL> "$LCB"
cd "$LCB"
python3 -m venv .venv && .venv/bin/pip install -e .
```

**4. Optional: log in to GitHub** (lets setup read the repository's issues and PRs)

```bash
gh auth login
```

---

## Part B — Every new repository

**1. Clone the repository, with its full history**

```bash
git clone https://github.com/<owner>/<repo>.git ~/repos/<repo>
```

- Don't use `--depth 1`: bug fixes are found in the history.
- If the download breaks partway, retry with
  `git -c http.version=HTTP/1.1 clone ...`.

**2. Make sure Neo4j is running**

```bash
brew services start neo4j
```

**3. Run the setup**

```bash
cd "$LCB"
$PY -m lcb setup ~/repos/<repo> --replace --hook none
```

It builds the repository, loads it into the graph, reads its git history and GitHub
issues, registers the MCP server for it in Claude Code, and installs the VS Code
extension.

- `--replace` swaps out whatever repository the graph holds now. The graph holds one
  repository at a time.
- The first build can take several minutes while Maven or Gradle downloads the
  project's libraries. Progress is printed as it goes.
- **It worked if** the final **Setup summary** shows `[ok]` lines. An `[!!]` line says
  what to do instead.

**4. Use it**

| Where | How |
|---|---|
| **VS Code** | Quit VS Code fully (Cmd+Q) and reopen it, then **File → Open Folder** → the repository. Edit a method and save: methods with past fixes are underlined, with details on hover. |
| **Claude Code** | `cd ~/repos/<repo> && claude`, then ask about the code: "who calls X?", "has Y been fixed before?", "which JAR provides Z?". It checks its own edits with `check_changes`. |
| **Terminal** | `$PY -m lcb check --repo ~/repos/<repo>` lists changed methods with past fixes. |
| **Git hook** (optional) | `$PY -m lcb hook install --repo ~/repos/<repo>` warns on commit (add `--block` to stop the commit). Remove it with `hook uninstall`. |

To find methods worth testing the warning on:

```bash
cd "$LCB"
./cq "MATCH (c:Commit)-[f:FIXED]->(m:Method) WHERE f.confidence >= 0.2 RETURN m.owner, m.name, c.message LIMIT 10;"
```

---

## If the build fails

The setup stops, shows the first errors, and gives the path of the full build log.

| Problem | What to do |
|---|---|
| The build needs a profile or flag | Add `--build-arg=-Pdev` to the setup command (repeatable) |
| Wrong Java version | Add `--jdk /path/to/jdk-home` |
| The build only works your own way | Build it yourself, then rerun the setup with `--skip-build` |
| `no mvnw ... and no mvn on PATH` | `brew install maven` (or `brew install gradle`) |
| Stuck downloading for a long time | It gives up on a stalled download after 60 s and retries; on a very slow connection, let it run |

The setup automatically builds with the right Java version, skips steps that aren't
needed (tests, Docker images, style checks, javadoc), and shows progress.

---

## Keeping it up to date

After `git pull` in the repository:

```bash
cd "$LCB"
$PY -m lcb ingest ~/repos/<repo>
```

Only what changed is reprocessed, so this usually takes seconds to a few minutes.

## Switching to another repository

Run Part B again for the other repository. `--replace` swaps it into the graph. For
example, back to the bundled example:

```bash
$PY -m lcb setup targets/petclinic --replace --hook none
```

## Useful extras

```bash
$PY -m lcb mcp-config --repo ~/repos/<repo>   # print the MCP config for other AI tools (Cursor, …)
./demo/start.sh                               # check Neo4j and the graph are healthy
```
