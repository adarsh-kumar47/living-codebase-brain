"""Tests for `lcb ingest` (lcb/ingest.py) and the multi-module extractor.

The end-to-end test takes the scripted two-module repo from test_git_layer
(core/ and web/), compiles each module into its own target/classes -- as a Maven
multi-module build would -- and adds a dependency JAR that contains a copy of a core
class, as a sibling module's artifact on the classpath does. `lcb ingest --skip-build`
must index both modules as application code, index the JAR copy zero times, attach
git history across both modules, and refuse to wipe a graph that holds another
repository unless told to.

    PYTHONPATH=. .venv-mac/bin/python -m unittest tests.test_ingest -v
"""

import contextlib
import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from lcb.generated import find_generated, marker_reason
from lcb.load import resolve_inherited
from lcb.ingest import (
    BuildProgress, MAVEN_SKIP_FLAGS, build, build_commands, choose_jdk, detect_build_tool,
    find_classes_dirs, find_jdks, jdk_major, mcp_config, required_java, run_build,
)

from tests.test_git_layer import (
    ALICE, CALC, CORE_STR, CTRL, ROOT, WEB_STR, FakeRepo, build_history, find_neo4j,
)
from tests.neo4j_sandbox import Neo4jSandbox


def touch(root, *rels):
    for rel in rels:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lcb-discover-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def rel(self, dirs):
        return sorted(os.path.relpath(d, self.root) for d in dirs)

    def test_maven_modules_without_test_output(self):
        touch(self.root,
              "pom.xml",
              "core/target/classes/a/A.class",
              "core/target/test-classes/a/ATest.class",
              "web/target/classes/b/B.class",
              "web/target/classes/META-INF/x/target/classes/Nested.class",  # inside output: not a module
              "ui/node_modules/pkg/target/classes/X.class")
        self.assertEqual(self.rel(find_classes_dirs(self.root)), ["core/target/classes", "web/target/classes"])
        self.assertEqual(detect_build_tool(self.root), "maven")

    def test_gradle_main_output_for_each_language(self):
        touch(self.root,
              "settings.gradle.kts",
              "app/build/classes/java/main/a/A.class",
              "app/build/classes/kotlin/main/a/K.class",
              "app/build/classes/java/test/a/ATest.class",
              "lib/build/classes/java/main/b/B.class")
        self.assertEqual(self.rel(find_classes_dirs(self.root)),
                         ["app/build/classes/java/main", "app/build/classes/kotlin/main",
                          "lib/build/classes/java/main"])
        self.assertEqual(detect_build_tool(self.root), "gradle")

    def test_maven_preferred_when_both_and_none_when_neither(self):
        touch(self.root, "pom.xml", "build.gradle")
        self.assertEqual(detect_build_tool(self.root), "maven")
        self.assertIsNone(detect_build_tool(tempfile.mkdtemp()))

    def test_mcp_config_points_at_the_repo(self):
        mcp_json, claude_cmd = mcp_config("/work/shop")
        entry = mcp_json["mcpServers"]["living-codebase-brain"]
        self.assertEqual(entry["args"], ["-m", "lcb.mcp_server"])
        self.assertEqual(entry["env"]["LCB_REPO"], "/work/shop")
        self.assertIn("claude mcp add living-codebase-brain", claude_cmd)
        self.assertIn("LCB_REPO=/work/shop", claude_cmd)


class ResolveInheritedTest(unittest.TestCase):
    METHODS = [
        {"fqn": "p.PageMethod#startPage(I)I", "is_static": "true"},
        {"fqn": "p.Base#run()V", "is_static": "false"},
        {"fqn": "p.Repo#save(Ljava/lang/Object;)V", "is_static": "false"},     # interface default/abstract
        {"fqn": "p.Repo#of()Lp/Repo;", "is_static": "true"},                  # interface static
        {"fqn": "p.Own#own()V", "is_static": "false"},
    ]
    EXTENDS = [{"child": "p.PageHelper", "parent": "p.PageMethod"},
               {"child": "p.Mid", "parent": "p.Base"}, {"child": "p.Leaf", "parent": "p.Mid"}]
    IMPLEMENTS = [{"child": "p.OwnerRepo", "iface": "p.JpaRepo"}, {"child": "p.JpaRepo", "iface": "p.Repo"}]

    def call(self, dst):
        return {"src": "p.App#x()V", "dst": dst, "dst_owner": dst.split("#")[0]}

    def test_resolution(self):
        rows = [self.call("p.PageHelper#startPage(I)I"),           # static from superclass (mall)
                self.call("p.Leaf#run()V"),                        # two levels up
                self.call("p.OwnerRepo#save(Ljava/lang/Object;)V"),  # superinterface of superinterface
                self.call("p.OwnerRepo#of()Lp/Repo;"),             # interface static: not inherited
                self.call("p.Own#own()V"),                         # declared where named: unchanged
                self.call("java.lang.String#trim()Ljava/lang/String;")]  # not indexed: unchanged
        self.assertEqual(resolve_inherited(rows, self.METHODS, self.EXTENDS, self.IMPLEMENTS), 3)
        self.assertEqual([(r["dst"], r.get("via")) for r in rows], [
            ("p.PageMethod#startPage(I)I", "p.PageHelper"),
            ("p.Base#run()V", "p.Leaf"),
            ("p.Repo#save(Ljava/lang/Object;)V", "p.OwnerRepo"),
            ("p.OwnerRepo#of()Lp/Repo;", None),
            ("p.Own#own()V", None),
            ("java.lang.String#trim()Ljava/lang/String;", None),
        ])
        self.assertEqual(rows[0]["dst_owner"], "p.PageMethod")


class GeneratedCodeTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lcb-gen-")
        subprocess.run(["git", "init", "-q", self.root], check=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def files(self, mapping):
        for rel, text in mapping.items():
            write(self.root, rel, text)
        subprocess.run(["git", "-C", self.root, "add", "-A"], check=True)

    def test_generator_configs_name_generated_packages(self):
        self.files({
            "a/generatorConfig.xml": '<javaClientGenerator type="XMLMAPPER" targetPackage="x.mapper"/>',
            "b/pom.xml": "<configuration><modelPackage>x.api.model</modelPackage></configuration>",
            "c/build.gradle": 'openApiGenerate { apiPackage = "x.api.client" }',
            "d/jooq.xml": "<target><packageName>x.db</packageName></target>",
            "src/x/mapper/M.java": "package x.mapper;\npublic interface M {}",
            "src/x/api/model/Pet.java": "package x.api.model;\npublic class Pet {}",
            "src/x/api/client/PetApi.java": "package x.api.client;\npublic class PetApi {}",
            "src/x/db/tables/T.java": "package x.db.tables;\npublic class T {}",
            "src/x/service/S.java": "package x.service;\npublic class S {}",
        })
        got = find_generated(self.root, {
            "src/x/mapper/M.java": "x.mapper", "src/x/api/model/Pet.java": "x.api.model",
            "src/x/api/client/PetApi.java": "x.api.client", "src/x/db/tables/T.java": "x.db.tables",
            "src/x/service/S.java": "x.service"})
        self.assertEqual(sorted(got), ["src/x/api/client/PetApi.java", "src/x/api/model/Pet.java",
                                       "src/x/db/tables/T.java", "src/x/mapper/M.java"])

    def test_markers_and_paths(self):
        self.assertEqual(marker_reason('@Generated("protoc")\npublic class A {}'), "marked @Generated")
        self.assertEqual(marker_reason("/** @mbg.generated */\nclass A {}"), "marked @Generated")
        self.assertIn("DO NOT EDIT", marker_reason("// DO NOT EDIT\npackage a;\npublic class A {}"))
        # Not generated: JPA's @GeneratedValue, and "auto-generated" about data, below the header.
        self.assertIsNone(marker_reason("@Entity\npublic class A {\n  @Id @GeneratedValue Long id;\n}"))
        self.assertIsNone(marker_reason("public class A {\n  // the id is auto-generated by the database\n}"))
        self.files({"build/generated-sources/x/G.java": "package x;\nclass G {}"})
        self.assertIn("build/generated-sources/x/G.java",
                      find_generated(self.root, {"build/generated-sources/x/G.java": "x"}))


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class JavaChoiceTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lcb-java-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def fake_jdk(self, name, version):
        home = os.path.join(self.root, "jdks", name)
        write(home, "release", f'JAVA_VERSION="{version}"\nOS_NAME="Darwin"\n')
        write(home, "bin/javac", "")
        return home

    def test_version_from_release_file(self):
        self.assertEqual(jdk_major(self.fake_jdk("a", "21.0.12.1")), 21)
        self.assertEqual(jdk_major(self.fake_jdk("b", "1.8.0_392")), 8)
        self.assertEqual(jdk_major(self.fake_jdk("c", "27")), 27)
        self.assertIsNone(jdk_major(os.path.join(self.root, "nowhere")))

    def test_finds_jdks_once_each_java_home_first(self):
        j21, j27 = self.fake_jdk("jdk-21", "21.0.2"), self.fake_jdk("jdk-27", "27")
        os.makedirs(os.path.join(self.root, "jdks", "not-a-jdk"))
        os.symlink(j27, os.path.join(self.root, "jdks", "latest"))   # same JDK twice
        found = find_jdks(globs=[os.path.join(self.root, "jdks", "*")], env={"JAVA_HOME": j27, "PATH": ""})
        self.assertEqual(found, [(27, os.path.realpath(j27)), (21, os.path.realpath(j21))])

    def test_oldest_jdk_that_is_new_enough(self):
        jdks = [(27, "/j27"), (21, "/j21"), (11, "/j11"), (17, "/j17")]
        self.assertEqual(choose_jdk(17, jdks), (17, "/j17"))
        self.assertEqual(choose_jdk(18, jdks), (21, "/j21"))   # mall/Lombok: 21, not 27
        self.assertEqual(choose_jdk(None, jdks), (27, "/j27"))  # no requirement: JAVA_HOME / PATH
        self.assertIsNone(choose_jdk(30, jdks))
        self.assertIsNone(choose_jdk(17, []))

    def test_required_java_from_maven_and_gradle(self):
        write(self.root, "pom.xml", "<properties><java.version>17</java.version></properties>")
        write(self.root, "legacy/pom.xml", "<maven.compiler.source>1.8</maven.compiler.source>")
        write(self.root, "svc/pom.xml", "<configuration><release>21</release></configuration>")
        self.assertEqual(required_java(self.root), 21)  # the module that needs the most

        g = tempfile.mkdtemp(dir=self.root)
        write(g, "build.gradle.kts", "java { toolchain { languageVersion = JavaLanguageVersion.of(17) } }")
        self.assertEqual(required_java(g), 17)
        g2 = tempfile.mkdtemp(dir=self.root)
        write(g2, "build.gradle", "sourceCompatibility = JavaVersion.VERSION_11")
        self.assertEqual(required_java(g2), 11)
        self.assertIsNone(required_java(tempfile.mkdtemp(dir=self.root)))


class BuildRunTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lcb-build-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_maven_command_skips_extras_and_times_out_downloads(self):
        write(self.root, "mvnw", "#!/bin/sh\n")
        cmd = build_commands(self.root, "maven", "/deps", ["-Pfast"])[0]
        for flag in ("-Ddocker.skip=true", "-Dcheckstyle.skip=true", "-DskipTests",
                     "-Daether.connector.requestTimeout=60000", "-Pfast"):
            self.assertIn(flag, cmd)
        self.assertNotIn("-q", cmd)  # progress must be visible
        self.assertEqual(cmd[-3:], ["package", "dependency:copy-dependencies", "-DoutputDirectory=/deps"])

    def test_maven_progress_lines(self):
        p = BuildProgress("maven")
        out = [m for line in [
            "[INFO] Building mall-common 1.0-SNAPSHOT                          [2/8]\n",
            *["[INFO] Downloaded from central: https://x/a.jar (3 kB at 1 kB/s)\n"] * 50,
            "[INFO] Compiling 12 source files\n",
            "[INFO] Building jar: /x/mall-common/target/mall-common-1.0-SNAPSHOT.jar\n",
            "[ERROR] /x/WebLogAspect.java:[68,19] cannot find symbol\n",
            "[INFO] BUILD FAILURE\n",
        ] for m in p.feed(line)]
        self.assertEqual(out[0], "building mall-common (2/8)")
        self.assertIn("downloaded 1 files (first build: fetching the project's libraries)", out)
        self.assertIn("downloaded 50 files", out)
        self.assertEqual(out[-1], "BUILD FAILURE")
        self.assertEqual(p.errors, ["[ERROR] /x/WebLogAspect.java:[68,19] cannot find symbol"])
        self.assertEqual(p.downloads, 50)

    def test_gradle_progress_lines(self):
        p = BuildProgress("gradle")
        out = [m for line in ["> Task :app:compileJava\n", "> Task :lib:compileKotlin\n",
                              "> Task :app:processResources\n", "BUILD SUCCESSFUL in 4s\n"]
               for m in p.feed(line)]
        self.assertEqual(out, ["compiling :app", "compiling :lib", "BUILD SUCCESSFUL in 4s"])

    def test_silence_prints_still_running(self):
        log = os.path.join(self.root, "build.log")
        script = "import time; print('[INFO] Building a 1 [1/1]', flush=True); time.sleep(2.5); print('[INFO] BUILD SUCCESS')"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code, _ = run_build([sys.executable, "-c", script], self.root, dict(os.environ), log, "maven", heartbeat=1)
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertIn("building a (1/1)", text)
        self.assertIn("still running, no output for", text)
        self.assertIn("BUILD SUCCESS", text)
        with open(log) as fh:
            self.assertIn("BUILD SUCCESS", fh.read())

    def test_failure_shows_errors_log_and_options(self):
        deps = os.path.join(self.root, "work", "deps")
        cmd = f"{shlex.quote(sys.executable)} -c \"import sys; print('error: boom in Foo.java'); sys.exit(3)\""
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as err:
            build(self.root, None, deps, build_cmd=cmd)
        msg = str(err.exception)
        self.assertIn("build failed (exit 3)", msg)
        self.assertIn("error: boom in Foo.java", msg)
        self.assertIn(os.path.join(self.root, "work", "build.log"), msg)
        self.assertIn("--build-arg", msg)

    def test_build_runs_with_the_chosen_jdk(self):
        deps = os.path.join(self.root, "work", "deps")
        cmd = f"{shlex.quote(sys.executable)} -c \"import os; print('JH=' + os.environ['JAVA_HOME'])\""
        jdk = os.path.join(self.root, "jdk")
        write(jdk, "release", 'JAVA_VERSION="21"\n')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            build(self.root, None, deps, build_cmd=cmd, jdk=jdk)
        self.assertIn(f"java 21 ({os.path.realpath(jdk)})", out.getvalue())
        with open(os.path.join(self.root, "work", "build.log")) as fh:
            self.assertEqual(fh.read().strip(), f"JH={os.path.realpath(jdk)}")


# Added on top of build_history(): a MyBatis-style generated model (named by a generator
# config, no marker in the file), and a call to a static method inherited from a
# superclass -- PageHelper.startPage, as in macrozheng/mall.
GEN_CONFIG = "core/src/main/resources/generatorConfig.xml"
ORDER = "core/src/main/java/com/acme/core/model/Order.java"
PAGE_METHOD = "web/src/main/java/com/acme/web/paging/PageMethod.java"
PAGE_HELPER = "web/src/main/java/com/acme/web/paging/PageHelper.java"
LISTER = "web/src/main/java/com/acme/web/paging/Lister.java"
EXTRA = {
    GEN_CONFIG: '<generatorConfiguration><context><javaModelGenerator targetPackage="com.acme.core.model" '
                'targetProject="src/main/java"/></context></generatorConfiguration>\n',
    ORDER: "package com.acme.core.model;\n\npublic class Order {\n    private Long id;\n\n"
           "    public Long getId() {\n        return id;\n    }\n}\n",
    PAGE_METHOD: "package com.acme.web.paging;\n\npublic class PageMethod {\n"
                 "    public static int startPage(int n) {\n        int p = n;\n        return p;\n    }\n}\n",
    PAGE_HELPER: "package com.acme.web.paging;\n\npublic class PageHelper extends PageMethod {\n}\n",
    LISTER: "package com.acme.web.paging;\n\nimport com.acme.core.model.Order;\n\npublic class Lister {\n"
            "    public int list() {\n        int x = PageHelper.startPage(1);\n"
            "        Long id = new Order().getId();\n        return x;\n    }\n}\n",
}


@unittest.skipUnless(find_neo4j() and shutil.which("javac") and shutil.which("jar"),
                     "needs a local Neo4j install and a JDK")
class IngestEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lcb-ingest-")
        cls.repo = FakeRepo(os.path.join(cls.tmp, "shop"))
        build_history(cls.repo)
        for rel, text in EXTRA.items():
            cls.repo.write(rel, text)
        cls.repo.commit("c8", ALICE, "add paging and the order model")
        cls.compile_modules(cls.repo.path)
        # A dependency JAR holding a copy of core's Calculator, as a sibling module's
        # packaged artifact on the classpath would.
        cls.deps = os.path.join(cls.tmp, "deps")
        os.makedirs(cls.deps)
        subprocess.run(["jar", "cf", os.path.join(cls.deps, "core-1.0.jar"), "-C",
                        os.path.join(cls.repo.path, "core", "target", "classes"), "."], check=True)
        cls.sandbox = Neo4jSandbox().start()
        cls.env = {"NEO4J_URI": cls.sandbox.uri, "PYTHONPATH": ROOT, "LCB_HOME": cls.tmp}
        cls.first = cls.ingest(cls.repo.path, "--jars", cls.deps)
        from neo4j import GraphDatabase
        cls.driver = GraphDatabase.driver(cls.sandbox.uri, auth=("neo4j", "unused"))

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "driver", None):
            cls.driver.close()
        if getattr(cls, "sandbox", None):
            cls.sandbox.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def compile_modules(repo):
        core_out = os.path.join(repo, "core", "target", "classes")
        web_out = os.path.join(repo, "web", "target", "classes")
        subprocess.run(["javac", "-d", core_out, *(os.path.join(repo, p) for p in (CALC, CORE_STR, ORDER))],
                       check=True)
        subprocess.run(["javac", "-cp", core_out, "-d", web_out,
                        *(os.path.join(repo, p) for p in (CTRL, WEB_STR, PAGE_METHOD, PAGE_HELPER, LISTER))],
                       check=True)

    @classmethod
    def ingest(cls, repo, *flags, expect_ok=True):
        proc = subprocess.run([sys.executable, "-m", "lcb", "ingest", repo, "--skip-build", "--no-github", *flags],
                              cwd=cls.tmp, capture_output=True, text=True, env={**os.environ, **cls.env})
        if expect_ok and proc.returncode != 0:
            raise AssertionError(f"lcb ingest failed:\n{proc.stdout}\n{proc.stderr}")
        return proc

    def query(self, cypher, **params):
        return [r.data() for r in self.driver.execute_query(cypher, params).records]

    def test_both_modules_are_application_code(self):
        rows = self.query("MATCH (c:Class) WHERE c.fqn STARTS WITH 'com.acme' "
                          "RETURN c.fqn AS fqn, c.is_external AS ext ORDER BY fqn")
        self.assertEqual(rows, [
            {"fqn": "com.acme.core.Calculator", "ext": False},
            {"fqn": "com.acme.core.model.Order", "ext": False},
            {"fqn": "com.acme.core.util.Strings", "ext": False},
            {"fqn": "com.acme.web.Controller", "ext": False},
            {"fqn": "com.acme.web.paging.Lister", "ext": False},
            {"fqn": "com.acme.web.paging.PageHelper", "ext": False},
            {"fqn": "com.acme.web.paging.PageMethod", "ext": False},
            {"fqn": "com.acme.web.util.Strings", "ext": False},
        ])
        self.assertIn("classes: core/target/classes", self.first.stdout)
        self.assertIn("classes: web/target/classes", self.first.stdout)

    def test_jar_copy_of_a_module_class_is_not_indexed_again(self):
        stats = os.path.join(self.tmp, "out", "repos")
        csv_dir = next(os.path.join(stats, d, "csv") for d in os.listdir(stats))
        with open(os.path.join(csv_dir, "classes.csv"), encoding="utf-8") as fh:
            calc_rows = [ln for ln in fh if ln.startswith('"com.acme.core.Calculator"')]
        self.assertEqual(len(calc_rows), 1)
        with open(os.path.join(csv_dir, "extract_stats.json"), encoding="utf-8") as fh:
            # The JAR holds core's three classes: Calculator, core.util.Strings, model.Order.
            self.assertIn('"jar_classes_shadowed_by_app": 3', fh.read())

    def test_cross_module_calls_and_history(self):
        calls = self.query("MATCH (a:Method {name:'handle'})-[:CALLS]->(b:Method {name:'add'}) RETURN b.owner AS o")
        self.assertEqual(calls, [{"o": "com.acme.core.Calculator"}])
        touched = self.query("MATCH (c:Commit)-[:TOUCHED]->(m:Method) RETURN DISTINCT m.owner AS o ORDER BY o")
        self.assertEqual({r["o"] for r in touched},
                         {"com.acme.core.Calculator", "com.acme.core.util.Strings",
                          "com.acme.web.Controller", "com.acme.web.util.Strings",
                          "com.acme.web.paging.Lister", "com.acme.web.paging.PageMethod",
                          "com.acme.web.paging.PageHelper"})  # not model.Order: generated
        fixed = self.query("MATCH (:Commit)-[:FIXED]->(m:Method) RETURN m.owner + '.' + m.name AS m ORDER BY m")
        self.assertEqual([r["m"] for r in fixed], ["com.acme.core.Calculator.sub", "com.acme.web.util.Strings.id"])

    def test_generated_code_is_marked_and_gets_no_history(self):
        rows = self.query("MATCH (c:Class) WHERE c.fqn IN ['com.acme.core.model.Order', 'com.acme.core.Calculator'] "
                          "OPTIONAL MATCH (c)-[:HAS_METHOD]->(m:Method)<-[t:TOUCHED]-() "
                          "RETURN c.fqn AS fqn, c.is_generated AS gen, count(t) AS touched ORDER BY fqn")
        self.assertEqual(rows, [{"fqn": "com.acme.core.Calculator", "gen": False, "touched": 5},
                                {"fqn": "com.acme.core.model.Order", "gen": True, "touched": 0}])
        gen_methods = self.query("MATCH (m:Method {owner: 'com.acme.core.model.Order'}) RETURN DISTINCT m.is_generated AS g")
        self.assertEqual(gen_methods, [{"g": True}])
        # Still in the call graph: application code calls generated code.
        self.assertEqual(self.query("MATCH (:Method {name:'list'})-[:CALLS]->(m:Method {name:'getId'}) RETURN m.owner AS o"),
                         [{"o": "com.acme.core.model.Order"}])
        self.assertIn("1 generated source file(s)", self.first.stdout)

    def test_call_to_inherited_static_method_reaches_the_declaring_class(self):
        rows = self.query("MATCH (:Method {name:'list'})-[c:CALLS]->(m:Method {name:'startPage'}) "
                          "RETURN m.owner AS owner, c.via AS via")
        self.assertEqual(rows, [{"owner": "com.acme.web.paging.PageMethod", "via": "com.acme.web.paging.PageHelper"}])

    def test_graph_records_its_repository(self):
        self.assertEqual(self.query("MATCH (m:LcbMeta) RETURN m.repo AS r"),
                         [{"r": os.path.realpath(self.repo.path)}])

    def test_next_steps_are_printed(self):
        self.assertIn("claude mcp add living-codebase-brain", self.first.stdout)
        self.assertIn(f"LCB_REPO={os.path.realpath(self.repo.path)}", self.first.stdout)
        self.assertIn("lcb hook install", self.first.stdout)

    def test_rerun_on_the_same_repo_is_incremental(self):
        out = self.ingest(self.repo.path, "--jars", self.deps).stdout
        self.assertIn("load into Neo4j (incremental)", out)
        self.assertIn("new_commits: 0", out.replace("git_new_commits", "new_commits"))

    def test_z_another_repo_needs_replace(self):
        other = FakeRepo(os.path.join(self.tmp, "other"))
        other.write("src/main/java/o/Only.java", "package o;\npublic class Only {\n  int v() {\n    return 1;\n  }\n}\n")
        other.commit("o1", ALICE, "init")
        subprocess.run(["javac", "-d", os.path.join(other.path, "target", "classes"),
                        os.path.join(other.path, "src/main/java/o/Only.java")], check=True)

        refused = self.ingest(other.path, expect_ok=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("One repository per graph", refused.stderr)
        self.assertEqual(self.query("MATCH (c:Class {fqn:'com.acme.core.Calculator'}) RETURN count(c) AS n"), [{"n": 1}])

        self.ingest(other.path, "--replace")
        self.assertEqual(self.query("MATCH (c:Class) WHERE c.is_external = false RETURN c.fqn AS f"), [{"f": "o.Only"}])
        self.assertEqual(self.query("MATCH (m:LcbMeta) RETURN m.repo AS r"), [{"r": os.path.realpath(other.path)}])
        self.assertEqual(self.query("MATCH (c:Commit) RETURN count(c) AS n"), [{"n": 1}])


if __name__ == "__main__":
    unittest.main()
