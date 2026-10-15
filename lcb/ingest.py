"""One command from a repository to a loaded graph.

    lcb ingest /path/to/repo

1. **Build**: detect Maven or Gradle (preferring the project's own `mvnw`/`gradlew`),
   compile every module, and collect the dependency JARs into one folder.
2. **Extract**: find every module's compiled-classes folder (`target/classes`,
   `build/classes/<lang>/main`) and run the bytecode extractor over all of them plus
   the dependencies.
3. **Load** into Neo4j (incremental when this repository is already the one loaded).
4. **Git history** (`lcb.git_ingest`), if the folder is a git repository.
5. **GitHub issues and PRs** (`lcb.github_ingest`), if `origin` is on GitHub and the
   `gh` CLI is logged in.

It then prints how to connect the MCP server, the git hook and the VS Code extension.

**One repository per graph.** Graph nodes carry no repository id, so the graph holds
one repository at a time. A `(:LcbMeta)` node records which. Ingesting a different
repository into a graph that already holds one is refused unless `--replace` is given,
so nobody's graph is wiped by accident.

The build runs with an explicit JAVA_HOME (the oldest installed JDK the project's build
files allow; `--jdk` overrides), skips steps that are not needed to get compiled
classes (tests, Docker images, style checks, docs, signing), gives up on stalled
downloads after 60 s instead of hanging, and shows its progress; the full output goes
to `out/repos/<repo>/build.log`. Extra flags go through with `--build-arg`. The build can
be skipped (`--skip-build`) when the project is already compiled, or replaced
(`--build-cmd "..."`).
Dependencies are then read from `--jars` and from the folder an earlier build filled.
"""

import argparse
import datetime
import glob
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from neo4j import GraphDatabase

from lcb.git_ingest import LCB_HOME, default_state_path, repo_key

URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("NEO4J_PASSWORD", "lcbpassword")

# Where the code lives (the extractor, the lcb package) -- fixed. LCB_HOME is where
# data goes (out/), which tests and users may point elsewhere.
CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRACTOR_SRC = os.path.join(CODE_ROOT, "extractor", "src", "Extractor.java")
EXTRACTOR_BUILD = os.path.join(CODE_ROOT, "extractor", "build")
ASM_JAR = os.path.join(CODE_ROOT, "extractor", "lib", "asm-9.7.1.jar")

SKIP_DIRS = {".git", ".gradle", ".idea", ".vscode", "node_modules", ".mvn", "dependency", "test-classes"}

# Registers `lcbCopyDeps` in every Java project: copies its compile and runtime
# classpath JARs into -Dlcb.deps. Gradle has no built-in equivalent of Maven's
# dependency:copy-dependencies.
GRADLE_INIT = """\
allprojects { p ->
    p.pluginManager.withPlugin('java') {
        p.tasks.register('lcbCopyDeps', Copy) {
            from(p.configurations.named('compileClasspath'))
            from(p.configurations.named('runtimeClasspath'))
            include('*.jar')
            into(System.getProperty('lcb.deps'))
            duplicatesStrategy = DuplicatesStrategy.EXCLUDE
        }
    }
}
"""


def step(msg):
    print(f"\n==> {msg}", flush=True)


# --- 1. build -----------------------------------------------------------------


def detect_build_tool(repo):
    """'maven' | 'gradle' | None. Maven wins when a project has both (PetClinic does)."""
    if os.path.exists(os.path.join(repo, "pom.xml")):
        return "maven"
    if any(os.path.exists(os.path.join(repo, f)) for f in
           ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")):
        return "gradle"
    return None


def wrapper_or(repo, wrapper, fallback):
    name = wrapper + (".cmd" if os.name == "nt" and wrapper == "mvnw" else
                      ".bat" if os.name == "nt" else "")
    path = os.path.join(repo, name)
    if os.path.exists(path):
        if os.name != "nt":
            os.chmod(path, os.stat(path).st_mode | 0o111)
        return path
    found = shutil.which(fallback)
    if not found:
        raise SystemExit(f"no {wrapper} in {repo} and no `{fallback}` on PATH")
    return found


# Build steps that produce something other than compiled classes: tests, Docker images,
# style and license checks, docs, signing, frontend bundles. Each is a common way for
# someone else's build to fail on this machine -- mall's docker plugin tried to reach
# the author's own Docker host -- and none of them is needed to read bytecode. A flag
# for a plugin a project doesn't use is simply ignored.
MAVEN_SKIP_FLAGS = [
    "-DskipTests",                                        # compile tests, don't run them
    "-Ddocker.skip=true", "-Ddockerfile.skip=true", "-Djib.skip=true",
    "-Dmaven.javadoc.skip=true", "-Dmaven.source.skip=true", "-Dgpg.skip=true",
    "-Dcheckstyle.skip=true", "-Dspotbugs.skip=true", "-Dpmd.skip=true", "-Dcpd.skip=true",
    "-Dspotless.check.skip=true", "-Dspring-javaformat.skip=true",
    "-Djacoco.skip=true", "-Dlicense.skip=true", "-Drat.skip=true",
    "-Denforcer.skip=true", "-Danimal.sniffer.skip=true",
    "-Dskip.npm", "-Dskip.yarn", "-Dskip.installnodenpm", "-Dskip.installyarn",
]
# A stalled download fails after 60 s and is retried, instead of hanging the build
# forever -- which happened twice on mall over a slow connection.
MAVEN_TIMEOUT_FLAGS = ["-Daether.connector.requestTimeout=60000",
                       "-Daether.connector.http.retryHandler.count=5"]
GRADLE_TIMEOUT_FLAGS = ["-Dorg.gradle.internal.http.connectionTimeout=60000",
                        "-Dorg.gradle.internal.http.socketTimeout=60000"]


def build_commands(repo, tool, deps_dir, extra_args=()):
    """The command(s) that compile every module and copy dependency JARs to deps_dir."""
    if tool == "maven":
        # `package`, not `compile`: in a multi-module build, copy-dependencies has to
        # resolve sibling modules as packaged JARs.
        return [[wrapper_or(repo, "mvnw", "mvn"), "-B", *MAVEN_SKIP_FLAGS, *MAVEN_TIMEOUT_FLAGS,
                 *extra_args, "package", "dependency:copy-dependencies", f"-DoutputDirectory={deps_dir}"]]
    init = os.path.join(tempfile.mkdtemp(prefix="lcb-gradle-"), "lcb-init.gradle")
    with open(init, "w", encoding="utf-8") as fh:
        fh.write(GRADLE_INIT)
    # Only `classes` and the copy task run, so check/test/docker tasks never start.
    return [[wrapper_or(repo, "gradlew", "gradle"), "--console=plain", "--init-script", init,
             *GRADLE_TIMEOUT_FLAGS, *extra_args, "classes", "lcbCopyDeps", f"-Dlcb.deps={deps_dir}"]]


# --- which Java to build with ---------------------------------------------------
#
# A build tool picks its own JDK: Homebrew's `mvn` uses Homebrew's newest JDK unless
# JAVA_HOME says otherwise. On mall that was Java 27, newer than its Lombok supports,
# so every Lombok-generated getter went missing. The build therefore always runs with
# an explicit JAVA_HOME: the oldest installed JDK that is at least what the project
# asks for -- new enough to compile it, old enough for its annotation processors.

JDK_GLOBS = (
    "/opt/homebrew/opt/openjdk*/libexec/openjdk.jdk/Contents/Home",
    "/usr/local/opt/openjdk*/libexec/openjdk.jdk/Contents/Home",
    "/Library/Java/JavaVirtualMachines/*/Contents/Home",
    "~/Library/Java/JavaVirtualMachines/*/Contents/Home",
    "/usr/lib/jvm/*",
    "~/.sdkman/candidates/java/*",
    "~/.jdks/*",
    "~/.asdf/installs/java/*",
    "C:/Program Files/Java/*",
    "C:/Program Files/Eclipse Adoptium/*",
)


def jdk_major(home):
    """Feature version from the JDK's `release` file: "21.0.2" -> 21, "1.8.0_392" -> 8."""
    try:
        with open(os.path.join(home, "release"), encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r'^JAVA_VERSION="(\d+)(?:\.(\d+))?', text, re.M)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major


def find_jdks(globs=JDK_GLOBS, env=os.environ):
    """[(major, home)] for every JDK with a compiler: JAVA_HOME first, then the one on
    PATH, then the usual install locations. Each home appears once."""
    homes = []
    if env.get("JAVA_HOME"):
        homes.append(env["JAVA_HOME"])
    javac = shutil.which("javac", path=env.get("PATH"))
    if javac:
        homes.append(os.path.dirname(os.path.dirname(os.path.realpath(javac))))
    for pattern in globs:
        homes += sorted(glob.glob(os.path.expanduser(pattern)))
    seen, jdks = set(), []
    for home in homes:
        real = os.path.realpath(home)
        javac_name = "javac.exe" if os.name == "nt" else "javac"
        if real in seen or not os.path.exists(os.path.join(real, "bin", javac_name)):
            continue
        seen.add(real)
        major = jdk_major(real)
        if major:
            jdks.append((major, real))
    return jdks


_JAVA_NUM = r"(?:1\.)?(\d+)"
MAVEN_JAVA_RES = [re.compile(p) for p in (
    rf"<java\.version>\s*{_JAVA_NUM}\s*</java\.version>",
    rf"<maven\.compiler\.(?:release|source|target)>\s*{_JAVA_NUM}\s*</",
    rf"<(?:release|source|target)>\s*{_JAVA_NUM}\s*</(?:release|source|target)>",
)]
GRADLE_JAVA_RES = [re.compile(p) for p in (
    r"JavaLanguageVersion\.of\(\s*(\d+)\s*\)",
    r"jvmToolchain\(\s*(\d+)\s*\)",
    r"JavaVersion\.VERSION_(?:1_)?(\d+)",
    rf"(?:source|target)Compatibility\s*=\s*['\"]?{_JAVA_NUM}",
    r"release(?:\.set\(|\s*=\s*)\s*(\d+)",
)]


def required_java(repo):
    """The highest Java version the build files ask for, or None. Every pom.xml and
    Gradle build script counts, since one module may need more than the rest."""
    found = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d not in ("target", "build", "src")]
        for f in files:
            res = MAVEN_JAVA_RES if f == "pom.xml" else \
                GRADLE_JAVA_RES if f.endswith((".gradle", ".gradle.kts")) else None
            if not res:
                continue
            with open(os.path.join(root, f), encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
            found += [int(m) for r in res for m in r.findall(text) if 5 <= int(m) <= 40]
    return max(found) if found else None


def choose_jdk(required, jdks):
    """(major, home) to build with, or None. The oldest JDK that is at least `required`;
    with no requirement, the first found (JAVA_HOME, else the one on PATH)."""
    if not jdks:
        return None
    if required is None:
        return jdks[0]
    ok = [j for j in jdks if j[0] >= required]
    return min(ok, key=lambda j: j[0]) if ok else None


# --- running the build, with progress -------------------------------------------


class BuildProgress:
    """Turns a build tool's output into a few lines worth printing: each module as it
    starts, a running download count, the result. Error lines are kept for the end."""

    # "Building mall-common 1.0-SNAPSHOT [2/8]" -- but not "Building jar: /path/x.jar",
    # which Maven also prints when it packages a module.
    MAVEN_MODULE = re.compile(r"^\[INFO\] Building (?!\w+: )(\S+) \S+(?:\s+\[(\d+)/(\d+)\])?")
    GRADLE_COMPILE = re.compile(r"^> Task (:?[\w.:-]*?):?compile(?:Java|Kotlin|Groovy|Scala)\b")

    def __init__(self, tool):
        self.tool = tool
        self.downloads = 0
        self.errors = []

    def feed(self, line):
        line = line.rstrip()
        out = []
        if self.tool == "maven":
            m = self.MAVEN_MODULE.match(line)
            if m:
                out.append(f"building {m.group(1)}" + (f" ({m.group(2)}/{m.group(3)})" if m.group(2) else ""))
            elif line.startswith("[INFO] Downloaded from"):
                self.downloads += 1
                if self.downloads == 1 or self.downloads % 50 == 0:
                    out.append(f"downloaded {self.downloads} files" +
                               (" (first build: fetching the project's libraries)" if self.downloads == 1 else ""))
            elif line.startswith("[ERROR]") and len(self.errors) < 200:
                self.errors.append(line)
        elif self.tool == "gradle":
            m = self.GRADLE_COMPILE.match(line)
            if m:
                out.append(f"compiling {m.group(1) or ':'}")
            elif ("What went wrong" in line or line.startswith(("e: ", "error:")) or ": error:" in line
                  or self.errors and len(self.errors) < 200 and line and not line.startswith("* Try")):
                self.errors.append(line)
        elif "error" in line.lower() and len(self.errors) < 200:
            self.errors.append(line)
        if re.search(r"BUILD (SUCCESS|FAILURE|SUCCESSFUL|FAILED)", line):
            out.append(line.replace("[INFO] ", "").replace("[ERROR] ", "").strip())
        return out


def run_build(cmd, cwd, env, log_path, tool, heartbeat=60):
    """Runs one build command, printing BuildProgress lines as they come and a
    "still running" line after each `heartbeat` seconds of silence. Everything goes to
    log_path. Returns (exit code, progress)."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", bufsize=1)
    lines = queue.Queue()
    threading.Thread(target=lambda: ([lines.put(ln) for ln in proc.stdout], lines.put(None)),
                     daemon=True).start()
    progress, quiet_since = BuildProgress(tool), time.time()
    with open(log_path, "w", encoding="utf-8") as log:
        while True:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                if time.time() - quiet_since >= heartbeat:
                    print(f"   ... still running, no output for {int(time.time() - quiet_since)}s "
                          f"(full log: {log_path})", flush=True)
                    quiet_since = time.time()
                continue
            if line is None:
                break
            log.write(line)
            quiet_since = time.time()
            for msg in progress.feed(line):
                print(f"   {msg}", flush=True)
    return proc.wait(), progress


def build(repo, tool, deps_dir, build_cmd=None, build_args=(), jdk=None, log_path=None):
    os.makedirs(deps_dir, exist_ok=True)
    env = dict(os.environ)
    required = required_java(repo)
    chosen = (jdk_major(jdk), os.path.realpath(jdk)) if jdk else choose_jdk(required, find_jdks())
    need = f"project needs Java {required}+" if required else "project does not say which Java it needs"
    if chosen:
        env["JAVA_HOME"] = chosen[1]
        env["PATH"] = os.path.join(chosen[1], "bin") + os.pathsep + env.get("PATH", "")
        print(f"   java {chosen[0]} ({chosen[1]}); {need}", flush=True)
    else:
        print(f"   warning: no installed JDK satisfies it ({need}); building with the default. "
              f"Install a matching JDK, or pass --jdk <path>.", flush=True)

    log_path = log_path or os.path.join(os.path.dirname(deps_dir), "build.log")
    cmds = [shlex.split(build_cmd)] if build_cmd else build_commands(repo, tool, deps_dir, build_args)
    for cmd in cmds:
        print("   $ " + " ".join(shlex.quote(c) for c in cmd), flush=True)
        t0 = time.time()
        code, progress = run_build(cmd, repo, env, log_path, tool if not build_cmd else None)
        if code != 0:
            shown = list(dict.fromkeys(e for e in progress.errors if e.strip()))[:25]
            raise SystemExit(
                "\n".join(["", f"build failed (exit {code}). First errors:", *("   " + e for e in shown),
                           f"Full log: {log_path}",
                           "Options: --build-arg <flag> to pass a flag to the build (repeatable), "
                           "--jdk <path> to pick the Java, --skip-build after compiling it yourself, "
                           "or --build-cmd for a custom build."]))
        print(f"   built in {time.time() - t0:.0f}s ({progress.downloads} files downloaded)", flush=True)


# --- 2. extract ---------------------------------------------------------------


def find_classes_dirs(repo):
    """Every module's main compiled-classes folder: Maven `<module>/target/classes`,
    Gradle `<module>/build/classes/<language>/main`. Test output is never included."""
    found = []
    for root, dirs, _ in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        parts = root.replace(os.sep, "/").split("/")
        if parts[-2:] == ["target", "classes"] or (
                len(parts) >= 4 and parts[-4:-2] == ["build", "classes"] and parts[-1] == "main"):
            found.append(root)
            dirs[:] = []  # its contents are packages, not more modules
    return found


def ensure_extractor():
    built = os.path.join(EXTRACTOR_BUILD, "Extractor.class")
    if os.path.exists(built) and os.path.getmtime(built) >= os.path.getmtime(EXTRACTOR_SRC):
        return
    if not shutil.which("javac"):
        raise SystemExit("javac not found: install a JDK (21+) to build the extractor")
    print("   compiling the extractor")
    subprocess.run(["javac", "-cp", ASM_JAR, "-d", EXTRACTOR_BUILD, EXTRACTOR_SRC], check=True)


def extract(classes_dirs, jars_dirs, csv_dir):
    ensure_extractor()
    cmd = ["java", "-cp", os.pathsep.join([ASM_JAR, EXTRACTOR_BUILD]), "Extractor", "--out", csv_dir]
    for d in classes_dirs:
        cmd += ["--classes", d]
    for d in jars_dirs:
        cmd += ["--jars", d]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"extractor failed:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


# --- 3. which repository the graph holds --------------------------------------


def graph_state(session):
    classes = session.run("MATCH (c:Class) RETURN count(c) AS n").single()["n"]
    meta = session.run("MATCH (m:LcbMeta) RETURN m.repo AS repo LIMIT 1").single()
    return classes, (meta["repo"] if meta else None)


def mark_graph(session, repo_root):
    session.run(
        "MERGE (m:LcbMeta {id: 'graph'}) SET m.repo = $repo, m.ingested_at = $at",
        repo=repo_root,
        at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


# --- 4/5. history ---------------------------------------------------------------


def github_slug_or_reason(repo):
    from lcb.github_ingest import slug_from_remote
    try:
        slug = slug_from_remote(repo)
    except subprocess.CalledProcessError:
        return None, "no 'origin' remote"
    except SystemExit as e:
        return None, str(e)
    if not shutil.which("gh"):
        return None, "the `gh` CLI is not installed"
    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        return None, "`gh` is not logged in (run `gh auth login`)"
    return slug, None


# --- next steps -------------------------------------------------------------------


def mcp_config(repo_root):
    """How to register the MCP server for this repository, for .mcp.json and for
    `claude mcp add`."""
    env = {
        "PYTHONPATH": CODE_ROOT,
        "NEO4J_URI": URI,
        "NEO4J_USER": USER,
        "NEO4J_PASSWORD": PASSWORD,
        "LCB_REPO": repo_root,
    }
    entry = {"command": sys.executable, "args": ["-m", "lcb.mcp_server"], "env": env}
    claude = ["claude", "mcp", "add", "living-codebase-brain"]
    for k, v in env.items():
        claude += ["-e", f"{k}={v}"]
    claude += ["--", sys.executable, "-m", "lcb.mcp_server"]
    return {"mcpServers": {"living-codebase-brain": entry}}, " ".join(shlex.quote(c) for c in claude)


def print_next_steps(repo_root):
    mcp_json, claude_cmd = mcp_config(repo_root)
    q = shlex.quote
    print(f"""
==> Done. Connect it:

  AI assistant (MCP) -- Claude Code:
    {claude_cmd}
  or add to the project's .mcp.json:
{json.dumps(mcp_json, indent=2)}

  Git hook (warn on commit):
    {q(sys.executable)} -m lcb hook install --repo {q(repo_root)}

  VS Code: install vscode-extension/living-codebase-brain-*.vsix, then in settings:
    "lcb.projectRoot": {json.dumps(CODE_ROOT)},
    "lcb.repo": {json.dumps(repo_root)}
""")


# --- entry point ------------------------------------------------------------------


def ingest(repo, skip_build=False, build_cmd=None, tool=None, jars=(), replace=False,
           git=True, github=True, build_args=(), jdk=None):
    repo_root = os.path.realpath(repo)
    if not os.path.isdir(repo_root):
        raise SystemExit(f"not a directory: {repo}")
    work = os.path.join(LCB_HOME, "out", "repos", repo_key(repo_root))
    deps_dir, csv_dir = os.path.join(work, "deps"), os.path.join(work, "csv")
    print(f"repository: {repo_root}\nworking dir: {work}")

    # Refuse before spending minutes on a build that would only be thrown away.
    with GraphDatabase.driver(URI, auth=(USER, PASSWORD), notifications_min_severity="OFF") as d, d.session() as s:
        classes, held = graph_state(s)
    if classes and held != repo_root and not replace:
        what = f"the graph already holds {held}" if held else \
            f"the graph already holds {classes:,} classes from an unrecorded repository"
        raise SystemExit(f"{what}. One repository per graph: rerun with --replace to wipe it "
                         f"and load {repo_root}, or point NEO4J_URI at another database.")
    fresh = not classes or held != repo_root

    tool = tool or detect_build_tool(repo_root)
    if not skip_build:
        if not tool and not build_cmd:
            raise SystemExit("no pom.xml or build.gradle found: compile the project yourself and "
                             "rerun with --skip-build, or pass --build-cmd")
        step(f"1/5 build ({'custom command' if build_cmd else tool})")
        build(repo_root, tool, deps_dir, build_cmd, build_args, jdk)
    else:
        step("1/5 build skipped")

    step("2/5 extract")
    classes_dirs = find_classes_dirs(repo_root)
    if not classes_dirs:
        raise SystemExit("no compiled classes found (looked for target/classes and "
                         "build/classes/*/main): has the project been built?")
    for c in classes_dirs:
        print(f"   classes: {os.path.relpath(c, repo_root)}")
    jars_dirs = [d for d in (deps_dir, *jars) if os.path.isdir(d)]
    n_jars = sum(1 for d in jars_dirs for f in os.listdir(d) if f.endswith(".jar"))
    print(f"   dependency JARs: {n_jars}")
    stats = extract(classes_dirs, jars_dirs, csv_dir)
    print(f"   {stats['classes']:,} classes, {stats['methods']:,} methods, "
          f"{stats['call_sites']:,} call sites")

    step(f"3/5 load into Neo4j ({'fresh' if fresh else 'incremental'})")
    from lcb import load as loader
    loaded = loader.load(csv_dir, wipe=fresh)
    with GraphDatabase.driver(URI, auth=(USER, PASSWORD), notifications_min_severity="OFF") as d, d.session() as s:
        mark_graph(s, repo_root)
    summary = {"classes_dirs": len(classes_dirs), "dependency_jars": n_jars, **{
        k: loaded[k] for k in ("calls_linked", "call_sites_total") if k in loaded}}

    is_git = subprocess.run(["git", "-C", repo_root, "rev-parse", "--git-dir"],
                            capture_output=True).returncode == 0
    if git and is_git:
        step("4/5 git history")
        shallow = subprocess.run(["git", "-C", repo_root, "rev-parse", "--is-shallow-repository"],
                                 capture_output=True, text=True).stdout.strip() == "true"
        if shallow:
            print("   note: shallow clone -- only the fetched history is ingested "
                  "(`git fetch --unshallow` for all of it)")
        from lcb import git_ingest
        summary.update({f"git_{k}": v for k, v in git_ingest.ingest(
            repo_root, wipe=fresh, state_path=default_state_path(repo_root), full=fresh).items()
            if k in ("new_commits", "authors", "touched_edges_total", "fixed_edges")})
    else:
        step(f"4/5 git history skipped ({'--no-git' if not git else 'not a git repository'})")

    slug, reason = github_slug_or_reason(repo_root) if (github and is_git) else (None, "--no-github" if not github else "not a git repository")
    if slug:
        step(f"5/5 GitHub issues and PRs ({slug})")
        from lcb import github_ingest
        cache = os.path.join(LCB_HOME, "out", "github", slug.replace("/", "__"))
        gh = github_ingest.ingest(slug, cache, fresh, os.path.join(cache, "state.json"), False)
        summary.update({f"github_{k}": v for k, v in gh.items() if k in ("issues", "prs", "caused_by_edges")})
    else:
        step(f"5/5 GitHub skipped ({reason}); fixes are still found from commit messages")

    print("\n==> Summary")
    for k, v in summary.items():
        print(f"   {k}: {v}")
    print_next_steps(repo_root)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lcb ingest",
                                 description="Build, extract, load and ingest history for one repository.")
    ap.add_argument("repo", help="path to the repository")
    ap.add_argument("--skip-build", action="store_true", help="use existing build output")
    ap.add_argument("--build-cmd", help="custom build command, run in the repository")
    ap.add_argument("--build-tool", choices=["maven", "gradle"], help="override detection")
    ap.add_argument("--jars", action="append", default=[], help="extra folder of dependency JARs (repeatable)")
    ap.add_argument("--replace", action="store_true",
                    help="wipe the graph if it holds a different repository")
    ap.add_argument("--no-git", action="store_true", help="skip git history")
    ap.add_argument("--no-github", action="store_true", help="skip GitHub issues and PRs")
    ap.add_argument("--build-arg", action="append", default=[], dest="build_args",
                    help="extra flag for the Maven/Gradle build, e.g. --build-arg=-Pprod (repeatable)")
    ap.add_argument("--jdk", help="JDK home to build with (default: the oldest installed JDK the "
                                  "project's build files allow)")
    args = ap.parse_args(argv)
    ingest(args.repo, args.skip_build, args.build_cmd, args.build_tool, args.jars,
           args.replace, not args.no_git, not args.no_github, args.build_args, args.jdk)
    return 0


if __name__ == "__main__":
    sys.exit(main())
